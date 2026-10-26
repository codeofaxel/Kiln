"""Many copies of a part, wherever they fit on the plate as it stands.

WHAT THIS PINS
--------------
The plate gate's fill branch (``_apply_plate_placement(copies=N)``), walked
through the slice step the print pipeline uses and through the two doors
that reach it (``multi_copy_print(fill=True)`` and ``kiln slice --fill``):

* an OCCUPIED plate asks the placement engine for the copies.  Its places
  are used exactly (each copy's footprint minimum corner on its ``at_mm``),
  the merged file is sliced and sent back for the second verdict, and the
  result says how many were placed.  Places withheld below the plan's tier
  refuse with the COUNT, naming the kiln-pro feature once.  No verdict
  refuses, fail closed; a no-room verdict refuses in its own sentence;
* a CLEAR or unrecorded plate is the free floor and never fails closed:
  the engine's places when it gives them, else a centred grid bounded by
  the bed and by any count the verdict gave;
* one copy is the one-part path; a 3MF is not filled.

The mutation pin (``TestTheEnginesPlacesAreUsed``): the engine's corners
are odd millimetres ([13, 17], [47, 17], [13, 61]) that a centred grid on
this 240 mm bed can never produce (a grid of three 20 mm cubes starts at
x = 80, y = 110), so a hook that ignored ``at_mm`` and gridded instead fails
there.  Run against such a mutation on 2026-10-25: both engine-position
tests failed, and passed again once it was restored.

The bridge and the slicer are stubbed; nothing here talks to a printer, a
slicer or the network.
"""

from __future__ import annotations

import struct
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from kiln import _pro_placement_bridge as bridge
from kiln import served_answer as sa
from kiln.plate_state import PlateJob, mark_clear, mark_occupied
from kiln.slicer import SliceResult

# ---------------------------------------------------------------------------
# Fixtures (the harness of test_plate_placement_doors.py)
# ---------------------------------------------------------------------------

_CUBE_TRIANGLES = 12


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


def _copy_boxes(path: str) -> list[tuple[float, float, float, float]]:
    """``(x0, y0, x1, y1)`` of each cube in a merged STL, in file order."""
    with open(path, "rb") as fh:
        fh.read(80)
        count = struct.unpack("<I", fh.read(4))[0]
        tris = []
        for _ in range(count):
            fh.read(12)
            tris.append([struct.unpack("<3f", fh.read(12)) for _ in range(3)])
            fh.read(2)
    assert count % _CUBE_TRIANGLES == 0, count
    boxes = []
    for i in range(0, count, _CUBE_TRIANGLES):
        verts = [v for tri in tris[i:i + _CUBE_TRIANGLES] for v in tri]
        xs, ys = [v[0] for v in verts], [v[1] for v in verts]
        boxes.append((round(min(xs), 3), round(min(ys), 3), round(max(xs), 3), round(max(ys), 3)))
    return boxes


def _machine() -> SimpleNamespace:
    return SimpleNamespace(
        name="bambu",
        serial="01P00A000000001",
        _printer_model="bambu_a1",
        get_state=lambda: SimpleNamespace(connected=True, state=SimpleNamespace(value="idle")),
        start_print=lambda *a, **k: pytest.fail("nothing here starts a print"),
    )


def _occupy(machine) -> None:
    mark_occupied(machine, PlateJob(file="jar_v2.gcode.3mf", footprint_mm=[90, 90, 160, 160], max_z_mm=42.0, printer_id="bambu_a1"))


ENGINE_CORNERS = [[13.0, 17.0], [47.0, 17.0], [13.0, 61.0]]


def _fill_verdict(
    corners: list[list[float]], *, fits: int | None = None, ok: bool = True, quiet_start: bool = True,
    wanted: int = 3, size: float = 20.0,
) -> dict:
    """A ``placement_verdict/1`` answering a request with ``copies``."""
    placed = [
        {"at_mm": list(c), "footprint_mm": [c[0], c[1], c[0] + size, c[1] + size], "clearance_mm": 8.0, "fault_risks": []}
        for c in corners
    ]
    first = corners[0] if corners else None
    return {
        "schema": bridge.SCHEMA, "ok": ok, "placed_by": "auto",
        "at_mm": list(first) if first else None, "tower_at_mm": None,
        "footprint_mm": [first[0], first[1], first[0] + size, first[1] + size] if first else None,
        "clearance_mm": 8.0 if first else None,
        "refusals": [] if ok else [{"code": "PLACEMENT_NO_ROOM", "sentence": "not one copy of this part fits beside the jar"}],
        "conflicts": [], "switched_off": {}, "spots": [], "spots_found": 0,
        "copies": {"wanted": wanted, "fits": len(corners) if fits is None else fits, "placed": placed},
        "occupancy": {
            "kind": bridge.OCCUPANCY_KIND, "bed_mm": [240.0, 240.0],
            "occupied": [{"name": "jar_v2.gcode.3mf", "rect_mm": [90, 90, 160, 160], "top_mm": 42.0}],
            "proposed": None, "copies": [{"rect_mm": p["footprint_mm"]} for p in placed] or None,
            "source": "gcode",
        },
        "record": {"printer_id": "bambu_a1", "measured": True, "source": "overlay", "quiet_start": quiet_start},
        "tier": {"verdict": "free", "plan": "pro"},
    }


class _Bridge:
    """A scripted bridge: one answer per ask, in order; the last one repeats."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.asked: list[dict] = []

    def __call__(self, request):
        self.asked.append(request)
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


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


def _slice_step(stl: str, machine, *, copies, spacing_mm: float = 10.0, placement=None):
    """The print pipeline's slice step, the one ``multi_copy_print(fill=True)`` reaches."""
    from kiln.pipelines import _slice_step as step_fn

    return step_fn(
        model_path=stl, effective_pid="ender3", printer_name=None, adapter=machine,
        placement=placement, profile=None, step_start=time.time(), copies=copies, spacing_mm=spacing_mm,
    )


def _refusal(step) -> dict:
    assert step.success is False, step
    return step.data or {}


# ---------------------------------------------------------------------------
# An occupied plate
# ---------------------------------------------------------------------------


class TestTheEnginesPlacesAreUsed:
    """MUTATION PIN: the corners are odd millimetres a centred grid on this
    bed can never produce, so a hook that grids instead of using ``at_mm``
    fails here (see the module docstring for the run that showed it)."""

    def test_occupied_three_copies_land_on_the_engines_corners_and_the_sliced_file_is_checked(self, tmp_path, machine, monkeypatch):
        _occupy(machine)
        asked = _Bridge((_fill_verdict(ENGINE_CORNERS), None))
        monkeypatch.setattr(bridge, "ask", asked)
        stl = _cube(tmp_path / "part.stl", off=(100.0, 100.0, 0.0))
        spy, gcode = _fake_slice(tmp_path)
        with patch("kiln.slicer.slice_file", spy):
            step, out = _slice_step(stl, machine, copies=3)

        assert step.success is True, step.message
        assert out == gcode
        sliced = spy.call_args.args[0]
        assert sliced != stl
        assert _copy_boxes(sliced) == [(13.0, 17.0, 33.0, 37.0), (47.0, 17.0, 67.0, 37.0), (13.0, 61.0, 33.0, 81.0)]
        # The envelope, then the sliced file: the same request, re-asked.
        assert len(asked.asked) == 2
        assert asked.asked[0]["copies"] == 3 and asked.asked[0]["placement"] == "auto"
        assert asked.asked[0]["sliced_gcode"] is None
        assert asked.asked[1]["sliced_gcode"] == {"path": gcode}
        assert step.data["copies"] == {"wanted": 3, "placed": 3, "basis": "engine"}
        assert step.data["approval_carries"] is False
        assert "3 copies placed beside jar v2" in step.data["approval_note"]

    def test_clear_plate_with_places_uses_the_engines_corners(self, tmp_path, machine, monkeypatch):
        mark_clear(machine, "human")
        asked = _Bridge((_fill_verdict(ENGINE_CORNERS), None))
        monkeypatch.setattr(bridge, "ask", asked)
        stl = _cube(tmp_path / "part.stl")
        spy, _ = _fake_slice(tmp_path)
        with patch("kiln.slicer.slice_file", spy):
            step, _out = _slice_step(stl, machine, copies=3)
        assert step.success is True, step.message
        assert [b[:2] for b in _copy_boxes(spy.call_args.args[0])] == [tuple(c) for c in ENGINE_CORNERS]
        assert step.data["copies"] == {"wanted": 3, "placed": 3, "basis": "engine"}
        assert len(asked.asked) == 1, "a clear plate is not checked a second time"


class TestAnOccupiedPlateRefuses:
    def test_places_withheld_refuse_with_the_count_and_name_the_feature_once(self, tmp_path, machine, monkeypatch):
        _occupy(machine)
        monkeypatch.setattr(bridge, "ask", _Bridge((_fill_verdict([], fits=4, wanted=6), None)))
        spy, _ = _fake_slice(tmp_path)
        with patch("kiln.slicer.slice_file", spy):
            step, out = _slice_step(_cube(tmp_path / "part.stl"), machine, copies=6)
        err = _refusal(step)
        assert out is None
        assert err["error"]["code"] == "PLACEMENT_COPIES_WITHHELD"
        message = err["error"]["message"]
        assert message.startswith("The last print, jar v2, is still on the plate (since ")
        assert "4 copies would fit beside it" in message
        assert message.count("kiln3d.com/pricing") == 1
        assert "clear the plate and say so" in message
        spy.assert_not_called()

    def test_places_withheld_where_no_start_would_stand_names_no_tier(self, tmp_path, machine, monkeypatch):
        _occupy(machine)
        monkeypatch.setattr(bridge, "ask", _Bridge((_fill_verdict([], fits=1, quiet_start=False), None)))
        spy, _ = _fake_slice(tmp_path)
        with patch("kiln.slicer.slice_file", spy):
            step, _out = _slice_step(_cube(tmp_path / "part.stl"), machine, copies=3)
        message = _refusal(step)["error"]["message"]
        assert "1 copy would fit beside it, but Kiln has no way to start a print there" in message
        assert "kiln3d.com" not in message and "kiln-pro" not in message
        spy.assert_not_called()

    @pytest.mark.parametrize("miss", [sa.Miss("offline"), sa.Miss("signed_out"), sa.Miss("unanswered")])
    def test_no_verdict_fails_closed(self, tmp_path, machine, monkeypatch, miss):
        from kiln import plate_state
        from kiln.plugins.slicer_tools import _no_verdict_sentence

        _occupy(machine)
        monkeypatch.setattr(bridge, "ask", _Bridge((None, miss)))
        spy, _ = _fake_slice(tmp_path)
        with patch("kiln.slicer.slice_file", spy):
            step, out = _slice_step(_cube(tmp_path / "part.stl"), machine, copies=3)
        err = _refusal(step)
        assert out is None
        assert err["error"]["code"] == "PLACEMENT_NO_VERDICT"
        assert err["error"]["message"] == _no_verdict_sentence(plate_state.read(machine), miss)
        spy.assert_not_called()

    def test_no_room_refuses_in_the_engines_sentence(self, tmp_path, machine, monkeypatch):
        _occupy(machine)
        monkeypatch.setattr(bridge, "ask", _Bridge((_fill_verdict([], fits=0, ok=False), None)))
        spy, _ = _fake_slice(tmp_path)
        with patch("kiln.slicer.slice_file", spy):
            step, _out = _slice_step(_cube(tmp_path / "part.stl"), machine, copies=3)
        err = _refusal(step)
        assert err["error"]["code"] == "PLACEMENT_NO_ROOM"
        assert "not one copy of this part fits beside the jar." in err["error"]["message"]
        spy.assert_not_called()


# ---------------------------------------------------------------------------
# A clear plate is the free floor
# ---------------------------------------------------------------------------


class TestAClearPlateIsTheFreeFloor:
    def test_no_answer_grids_as_many_as_asked_when_they_fit(self, tmp_path, machine, monkeypatch):
        mark_clear(machine, "human")
        monkeypatch.setattr(bridge, "ask", _Bridge((None, sa.Miss("offline"))))
        spy, _ = _fake_slice(tmp_path)
        with patch("kiln.slicer.slice_file", spy):
            step, _out = _slice_step(_cube(tmp_path / "part.stl"), machine, copies=5)
        assert step.success is True, step.message
        boxes = _copy_boxes(spy.call_args.args[0])
        assert len(boxes) == 5
        # A centred grid on the 240 mm bed: 5 x 20 mm + 4 x 10 mm = 140 mm wide.
        assert boxes[0][:2] == (50.0, 110.0) and boxes[-1][2:] == (190.0, 130.0)
        assert step.data["copies"] == {"wanted": 5, "placed": 5, "basis": "grid"}

    def test_the_grid_is_bounded_by_the_bed(self, tmp_path, machine, monkeypatch):
        mark_clear(machine, "human")
        monkeypatch.setattr(bridge, "ask", _Bridge((None, sa.Miss("offline"))))
        spy, _ = _fake_slice(tmp_path)
        with patch("kiln.slicer.slice_file", spy):
            step, _out = _slice_step(_cube(tmp_path / "part.stl", size=60.0), machine, copies=50)
        # (240 + 10) // (60 + 10) = 3 a side: 9, never refused for fewer than asked.
        assert step.success is True, step.message
        assert len(_copy_boxes(spy.call_args.args[0])) == 9
        assert step.data["copies"] == {"wanted": 50, "placed": 9, "basis": "grid"}

    def test_the_grid_is_bounded_by_the_count_the_verdict_gave(self, tmp_path, machine, monkeypatch):
        mark_clear(machine, "human")
        monkeypatch.setattr(bridge, "ask", _Bridge((_fill_verdict([], fits=2, wanted=6), None)))
        spy, _ = _fake_slice(tmp_path)
        with patch("kiln.slicer.slice_file", spy):
            step, _out = _slice_step(_cube(tmp_path / "part.stl"), machine, copies=6)
        assert step.success is True, step.message
        assert len(_copy_boxes(spy.call_args.args[0])) == 2
        assert step.data["copies"] == {"wanted": 6, "placed": 2, "basis": "grid"}

    def test_no_printer_at_all_grids_and_asks_nobody(self, tmp_path, monkeypatch):
        import kiln.server as srv

        def _no_printer(*_a, **_k):
            raise RuntimeError("no printer configured")

        monkeypatch.setattr(srv, "_resolve_adapter", _no_printer)
        monkeypatch.setattr(bridge, "ask", lambda request: pytest.fail("no printer means no plate to ask about"))
        spy, _ = _fake_slice(tmp_path)
        with patch("kiln.slicer.slice_file", spy):
            step, _out = _slice_step(_cube(tmp_path / "part.stl"), None, copies=4)
        assert step.success is True, step.message
        assert len(_copy_boxes(spy.call_args.args[0])) == 4
        assert step.data["copies"] == {"wanted": 4, "placed": 4, "basis": "grid"}

    def test_not_one_copy_fitting_refuses_with_the_sizes(self, tmp_path, machine, monkeypatch):
        mark_clear(machine, "human")
        monkeypatch.setattr(bridge, "ask", _Bridge((None, sa.Miss("offline"))))
        spy, _ = _fake_slice(tmp_path)
        with patch("kiln.slicer.slice_file", spy):
            step, _out = _slice_step(_cube(tmp_path / "part.stl", size=300.0), machine, copies=3)
        err = _refusal(step)
        assert err["error"]["code"] == "PLACEMENT_NO_ROOM"
        assert err["error"]["message"] == (
            "Not one copy of this part (300 x 300 mm) fits on the 240 x 240 mm bed with 10 mm between copies."
        )
        spy.assert_not_called()


# ---------------------------------------------------------------------------
# What is not a fill
# ---------------------------------------------------------------------------


class TestWhatIsNotAFill:
    def test_one_copy_is_the_one_part_path(self, tmp_path, machine, monkeypatch):
        mark_clear(machine, "human")
        monkeypatch.setattr(bridge, "ask", lambda request: pytest.fail("a clear plate asks nobody for one part"))
        stl = _cube(tmp_path / "part.stl", off=(10.0, 10.0, 0.0))
        spy, _ = _fake_slice(tmp_path)
        with patch("kiln.slicer.slice_file", spy):
            step, _out = _slice_step(stl, machine, copies=1)
        assert step.success is True
        assert spy.call_args.args[0] == stl
        assert "copies" not in step.data

    def test_a_3mf_is_not_filled(self, tmp_path, machine, monkeypatch):
        _occupy(machine)
        monkeypatch.setattr(bridge, "ask", lambda request: pytest.fail("a 3MF is refused before anyone is asked"))
        part = tmp_path / "part.3mf"
        part.write_bytes(b"PK\x03\x04")
        spy, _ = _fake_slice(tmp_path)
        with patch("kiln.slicer.slice_file", spy):
            step, _out = _slice_step(str(part), machine, copies=3)
        err = _refusal(step)
        assert err["error"]["code"] == "PLACEMENT_UNPLACEABLE"
        assert "copies of an STL" in err["error"]["message"] and "import_external_mesh" in err["error"]["message"]
        spy.assert_not_called()

    def test_copies_take_no_spot(self, tmp_path, machine, monkeypatch):
        _occupy(machine)
        monkeypatch.setattr(bridge, "ask", lambda request: pytest.fail("refused before anyone is asked"))
        spy, _ = _fake_slice(tmp_path)
        with patch("kiln.slicer.slice_file", spy):
            step, _out = _slice_step(_cube(tmp_path / "part.stl"), machine, copies=3, placement=[40, 40])
        assert _refusal(step)["error"]["code"] == "PLACEMENT_INVALID"
        spy.assert_not_called()

    def test_the_request_carries_copies_only_when_asked(self, machine):
        assert bridge.request_for(machine, "bambu_a1", placement="auto", part=None)["copies"] is None
        assert bridge.request_for(machine, "bambu_a1", placement="auto", part=None, copies=7)["copies"] == 7


# ---------------------------------------------------------------------------
# The doors
# ---------------------------------------------------------------------------


class TestMultiCopyPrintFill:
    @pytest.fixture(autouse=True)
    def _preview_gate_bypassed(self, monkeypatch):
        monkeypatch.setenv("KILN_SKIP_PREVIEW_GATE", "1")

    def test_fill_reaches_the_pipeline_with_copies_and_no_duplicate_flag(self, tmp_path):
        from kiln.server import multi_copy_print

        stl = _cube(tmp_path / "part.stl")
        result = MagicMock()
        result.success = True
        result.steps = [SimpleNamespace(name="slice", data={"copies": {"wanted": 30, "placed": 12, "basis": "engine"}})]
        result.to_dict.return_value = {"pipeline": "reslice_and_print", "success": True, "steps": []}
        with (
            patch("kiln.slicer.find_slicer", side_effect=AssertionError("fill picks no slicer strategy")),
            patch("kiln.server._pipeline_reslice_and_print", return_value=result) as pipeline,
        ):
            summary = multi_copy_print(model_path=stl, copies=30, spacing_mm=4.0, fill=True)
        kwargs = pipeline.call_args.kwargs
        assert kwargs["copies"] == 30 and kwargs["spacing_mm"] == 4.0
        assert "--duplicate" not in (kwargs.get("extra_args") or [])
        assert kwargs["model_path"] == stl
        assert summary["strategy"] == "fill"
        assert summary["copies_requested"] == 30 and summary["copies_placed"] == 12

    def test_without_fill_thirty_is_still_refused(self, tmp_path):
        from kiln.server import multi_copy_print

        summary = multi_copy_print(model_path=_cube(tmp_path / "part.stl"), copies=30)
        assert summary["success"] is False
        assert summary["error"]["code"] == "VALIDATION_ERROR"
        assert "copies must be <= 20" in summary["error"]["message"]

    def test_fill_caps_at_fifty(self, tmp_path):
        from kiln.server import multi_copy_print

        summary = multi_copy_print(model_path=_cube(tmp_path / "part.stl"), copies=51, fill=True)
        assert summary["error"]["code"] == "VALIDATION_ERROR" and "<= 50" in summary["error"]["message"]


class TestKilnSliceFill:
    def test_fill_needs_copies(self, tmp_path):
        from click.testing import CliRunner

        from kiln.cli.main import cli

        out = CliRunner().invoke(cli, ["slice", _cube(tmp_path / "part.stl"), "--fill"])
        assert out.exit_code == 2
        assert "--fill needs --copies N" in out.output

    def test_fill_hands_copies_to_the_plate_gate_never_the_slicer(self, tmp_path, monkeypatch):
        from click.testing import CliRunner

        import kiln.cli.main as cli_main

        seen: dict = {}

        def _fake_placed_slice(ctx, input_file, **kwargs):
            seen.update(kwargs, input_file=input_file)
            gcode = tmp_path / "out.gcode"
            gcode.write_text("G1 X1\n")
            res = SliceResult(success=True, output_path=str(gcode), slicer="prusa-slicer", message="Sliced")
            return res, {"plate": "clear", "fill": {"wanted": 8, "placed": 6, "basis": "grid"}}

        monkeypatch.setattr(cli_main, "_cli_placed_slice", _fake_placed_slice)
        monkeypatch.setattr(cli_main, "_resolve_slice_plan", lambda *a, **k: {
            "extra_args": [], "printer_id": None, "profile_path": None, "material": "PLA",
            "declared_material": None, "loaded_material": None, "loaded_determined_by": None,
        })
        monkeypatch.setattr("kiln.slicer.find_slicer", lambda *a, **k: pytest.fail("fill picks no slicer strategy"))
        stl = _cube(tmp_path / "part.stl")
        out = CliRunner().invoke(cli_main.cli, ["slice", stl, "--copies", "8", "--fill", "--spacing", "3"])
        assert out.exit_code == 0, out.output
        assert seen["copies"] == 8 and seen["spacing_mm"] == 3.0
        assert seen["input_file"] == stl
        assert "--duplicate" not in (seen.get("extra_args") or [])
        assert "Placed 6 of 8 copies" in out.output
