"""A print whose filament is not loaded says so once, and offers what to do.

Four situations, one sentence each, at every print door: the right colour in
the wrong plastic, the right plastic in the wrong colour, the plastic not
loaded at all, and one piece of a several-filament print missing.  Each
refusal carries ``options`` an agent acts on without reading the sentence:
``load_spool``, ``print_from_slot`` or ``reslice_for``, the slot as Studio
names it, the plastic and colour, and the door's own call.  A different
plastic is only offered with a re-slice; nothing starts on an offer.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from kiln import declared_color as dc
from kiln.printers.base import PrinterFile, PrinterState, PrinterStatus, PrintResult, UploadResult

WHITE, BLACK, GREY, RED, ORANGE, BLUE = "FFFFFFFF", "111111FF", "808080FF", "FF0000FF", "FFA500FF", "0000FFFF"


def _ams(*trays: tuple[str, str] | None) -> dict[str, Any]:
    """One four-slot unit; ``None`` is an empty slot."""
    rows = []
    for i, tray in enumerate(trays):
        kind, colour = tray if tray else ("", "00000000")
        rows.append({"slot": i, "tray_type": kind, "tray_color": colour, "remain": 50, "remaining_known": True})
    return {"tray_now": "255", "ams_exist_bits": "1", "tray_exist_bits": "f", "units": [{"unit_id": 0, "trays": rows}]}


#: White, black, grey and red PLA in A1-A4.
FOUR_PLA = _ams(("PLA", WHITE), ("PLA", BLACK), ("PLA", GREY), ("PLA", RED))
#: White, black and orange PLA in A1-A3.
NO_RED = _ams(("PLA", WHITE), ("PLA", BLACK), ("PLA", ORANGE))
#: White, black and red PLA in A1-A3; A4 empty.
NO_BLUE = _ams(("PLA", WHITE), ("PLA", BLACK), ("PLA", RED), None)

S1 = ("This print is set up for red PETG, but the only red spool loaded is red PLA in slot A4. "
      "PLA can't just stand in, because the file is sliced for PETG's temperatures. "
      "Want me to re-slice it for PLA and print from A4? Or load red PETG and I'll start it as is.")
S2 = ("This print wants red PLA. You have PLA loaded, but no red: the closest is orange in slot A3. "
      "Print it in orange, or load red first?")
S3 = ("This print needs PETG, and none of your spools are PETG (you have white, black, grey and red PLA). "
      "Load a PETG spool and I'll start it. Or I can re-slice it for PLA, if PLA suits what this part is for.")
S4 = ("Three of the four colours are loaded. The blue part needs blue PLA, and there's none loaded. "
      "Load blue into empty slot A4 and I'll start it.")


def _gcode(path: Path, colours: str, types: str) -> str:
    path.write_text(f"G28\nG1 X1 Y1\n; filament_colour = {colours}\n; filament_type = {types}\n")
    return str(path)


def _stl(path: Path) -> str:
    tris = [((0, 0, 0), (10, 0, 0), (10, 10, 0)), ((0, 0, 0), (10, 10, 0), (0, 10, 0)),
            ((0, 0, 5), (10, 10, 5), (10, 0, 5)), ((0, 0, 5), (0, 10, 5), (10, 10, 5))]
    with open(path, "wb") as fh:
        fh.write(b"\x00" * 80 + struct.pack("<I", len(tris)))
        for tri in tris:
            fh.write(struct.pack("<3f", 0, 0, 1))
            for v in tri:
                fh.write(struct.pack("<3f", *v))
            fh.write(struct.pack("<H", 0))
    return str(path)


def _by(options: list[dict[str, Any]], action: str) -> list[dict[str, Any]]:
    return [o for o in options if o["action"] == action]


class _Printer:
    """An idle Bambu-like printer with an AMS reading and a file list."""

    def __init__(self, reading: dict[str, Any], files: tuple[str, ...] = ()) -> None:
        self.reading = reading
        self.files = files
        self.started: list[tuple[str, dict[str, Any]]] = []
        self.name = "bambu"

    def get_ams_status(self) -> dict[str, Any]:
        return self.reading

    def get_state(self) -> PrinterState:
        return PrinterState(state=PrinterStatus.IDLE, connected=True, tool_temp_actual=22.0, tool_temp_target=0.0,
                            bed_temp_actual=21.0, bed_temp_target=0.0)

    def get_job(self) -> Any:
        return mock.MagicMock(file_name=None)

    def list_files(self) -> list[PrinterFile]:
        return [PrinterFile(name=f, path=f, size_bytes=1234) for f in self.files]

    def upload_file(self, path: str) -> UploadResult:
        return UploadResult(success=True, file_name=Path(path).name, message="uploaded")

    def start_print(self, file_name: str, **kwargs: Any) -> PrintResult:
        self.started.append((file_name, kwargs))
        return PrintResult(success=True, message=f"Started printing {file_name}.")

    def __getattr__(self, name: str) -> Any:
        if name.startswith("wrap_"):
            raise AttributeError(name)
        return mock.MagicMock()


@pytest.fixture(autouse=True)
def _gates(monkeypatch):
    monkeypatch.setenv("KILN_SKIP_PREVIEW_GATE", "1")
    monkeypatch.setattr("kiln.server._check_auth", lambda *a, **k: None)
    monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)


def _tool(name: str) -> Any:
    import kiln.server as srv

    return srv.mcp._tool_manager._tools[name].fn  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# start_print: a file already on the printer
# ---------------------------------------------------------------------------


class TestStartPrint:
    def _start(self, printer: _Printer, file_name: str, **kw: Any) -> dict[str, Any]:
        import kiln.server as srv

        with mock.patch.object(srv, "_get_adapter", return_value=printer), \
                mock.patch.object(srv, "_resolve_adapter", return_value=printer), \
                mock.patch.object(srv, "_check_rate_limit", return_value=None), \
                mock.patch.object(srv, "preflight_check", return_value={"ready": True}):
            return _tool("start_print")(file_name, **kw)

    def test_the_right_colour_in_the_wrong_plastic(self, tmp_path):
        part = _gcode(tmp_path / "part.gcode", "#FF0000", "PETG")
        dc.record(part, dc.ShownColour("red", "#FF0000"))
        printer = _Printer(FOUR_PLA, (part,))
        out = self._start(printer, part)
        assert out["success"] is False and printer.started == []
        assert out["error"]["code"] == "AMS_MATERIAL_NOT_LOADED"
        assert out["error"]["message"] == S1
        reslice, load = out["options"]
        assert reslice["action"] == "reslice_for" and reslice["slot"] == "A4" and reslice["tray_id"] == 3
        assert (reslice["material"], reslice["color"]) == ("PLA", "red")
        assert reslice["call"]["tool"] == "run_reslice_and_print"
        assert reslice["call"]["arguments"] == {"material": "pla", "ams_mapping": [3]}
        assert "model_path" in reslice["call"]["needs"]
        assert (load["action"], load["material"], load["color"], load["slot"]) == ("load_spool", "PETG", "red", None)

    def test_one_piece_missing_and_the_chosen_slot_then_prints(self, tmp_path):
        part = _gcode(tmp_path / "four.gcode", "#FFFFFF;#111111;#FF0000;#0000FF", "PLA;PLA;PLA;PLA")
        printer = _Printer(_ams(("PLA", WHITE), ("PLA", BLACK), ("PLA", RED), ("PLA", "00A000FF")), (part,))
        out = self._start(printer, part)
        assert out["success"] is False and printer.started == []
        assert out["error"]["code"] == "AMS_COLOR_MISMATCH"
        assert out["error"]["message"] == (
            "Three of the four colours are loaded. The blue part needs blue PLA, and there's none loaded. "
            "Load blue in place of a spool this print doesn't use, or pick a loaded colour for that part."
        )
        load = _by(out["options"], "load_spool")[0]
        assert (load["part"], load["material"], load["color"]) == (3, "PLA", "blue")
        green = _by(out["options"], "print_from_slot")[0]
        assert (green["slot"], green["color"], green["mapping"]) == ("A4", "green", [0, 1, 2, 3])
        assert green["call"] == {"tool": "start_print", "arguments": {"file_name": part, "ams_mapping": [0, 1, 2, 3]}}
        # The person picks green: the door prints from the trays the choice names.
        again = self._start(printer, **green["call"]["arguments"])
        assert again["success"] is True
        assert printer.started[0][1]["ams_mapping"] == [0, 1, 2, 3]


# ---------------------------------------------------------------------------
# slice_and_print: the model is sliced, then routed
# ---------------------------------------------------------------------------


def _slicer_tools() -> dict[str, Any]:
    from kiln.plugins.slicer_tools import _SlicerToolsPlugin

    tools: dict[str, Any] = {}

    class FakeMCP:
        def tool(self_mcp, name: str | None = None, **_kwargs):  # noqa: N805
            def decorator(fn):
                tools[name or fn.__name__] = fn
                return fn
            return decorator

    _SlicerToolsPlugin().register(FakeMCP())
    return tools


class TestSliceAndPrint:
    def _run(self, tmp_path: Path, reading: dict[str, Any], *, types: str, asked: str | None = None,
             **kw: Any) -> tuple[dict[str, Any], _Printer, str]:
        import kiln.plugins.slicer_tools as st
        import kiln.server as srv
        from kiln.slicer import SliceResult

        model = _stl(tmp_path / "part.stl")
        sliced = tmp_path / "part.gcode"
        ini = tmp_path / "profile.ini"
        ini.write_text("layer_height = 0.2\n")
        printer = _Printer(reading)

        def fake_slice_file(path, **_kwargs):
            _gcode(sliced, "#FFFFFF", types)
            if asked:
                dc.record(str(sliced), dc.ShownColour(asked, dc.hex_for(asked)))
            return SliceResult(success=True, output_path=str(sliced), slicer="prusa-slicer", message="ok")

        with mock.patch.object(srv, "_resolve_slice_profile_context", return_value=(None, str(ini))), \
                mock.patch.object(srv, "_PRINTER_TYPE", "bambu"), \
                mock.patch.object(srv, "_resolve_adapter", return_value=printer), \
                mock.patch.object(srv, "_resolve_effective_printer_name", return_value="p1"), \
                mock.patch.object(srv, "_emergency_latch_error", return_value=None), \
                mock.patch.object(srv, "preflight_check", return_value={"ready": True}), \
                mock.patch.object(srv, "_get_heater_watchdog", return_value=mock.MagicMock()), \
                mock.patch.object(srv, "_audit", mock.MagicMock()), \
                mock.patch.object(st, "_apply_bed_fit_gate", return_value=(model, None, {})), \
                mock.patch.object(st, "_multicolor_flatten_advisory", return_value=(None, None)), \
                mock.patch("kiln.slicer.slice_file", side_effect=fake_slice_file):
            out = _slicer_tools()["slice_and_print"](input_path=model, skip_validation=True, **kw)
        return out, printer, model

    def test_the_right_plastic_in_the_wrong_colour(self, tmp_path):
        out, printer, model = self._run(tmp_path, NO_RED, types="PLA", asked="red", material="PLA")
        assert out["success"] is False and printer.started == []
        assert out["error"]["code"] == "COLOR_NOT_LOADED"
        assert out["error"]["message"] == S2
        orange, load = out["options"]
        assert (orange["action"], orange["slot"], orange["tray_id"], orange["color"]) == ("print_from_slot", "A3", 2, "orange")
        assert orange["call"] == {"tool": "slice_and_print", "arguments": {
            "input_path": model, "material": "PLA", "color": "#FFA500"}}
        assert (load["action"], load["material"], load["color"]) == ("load_spool", "PLA", "red")
        assert out["colour"]["outcome"] == "held" and out["colour"]["message"] == S2

    def test_the_plastic_not_loaded(self, tmp_path):
        out, printer, model = self._run(tmp_path, FOUR_PLA, types="PETG", material="PETG")
        assert out["success"] is False and printer.started == []
        assert out["error"]["code"] == "AMS_MATERIAL_NOT_LOADED"
        assert out["error"]["message"] == S3
        load, reslice = out["options"]
        assert (load["action"], load["material"], load["color"]) == ("load_spool", "PETG", None)
        assert (reslice["action"], reslice["material"]) == ("reslice_for", "PLA")
        assert reslice["call"] == {"tool": "slice_and_print", "arguments": {"input_path": model, "material": "pla"}}
        assert "suits_goal" not in reslice


# ---------------------------------------------------------------------------
# run_quick_print: the pipeline's print step
# ---------------------------------------------------------------------------


class TestThePipeline:
    def test_the_plastic_not_loaded_in_the_one_shot_pipeline(self, tmp_path):
        import kiln.server as srv
        from kiln.slicer import SliceResult

        model = _stl(tmp_path / "part.stl")
        sliced = tmp_path / "part.gcode"
        printer = _Printer(FOUR_PLA)

        def fake_slice_file(path, **_kwargs):
            _gcode(sliced, "#FFFFFF", "PETG")
            return SliceResult(success=True, output_path=str(sliced), slicer="prusa-slicer", message="ok")

        with mock.patch("kiln.pipelines._resolve_pipeline_adapter", return_value=printer), \
                mock.patch("kiln.slicer.slice_file", side_effect=fake_slice_file), \
                mock.patch("kiln.pipelines._safety_check_step",
                           return_value=mock.MagicMock(success=True, name="safety_check", data={}, message="ok")), \
                mock.patch.object(srv, "_resolve_adapter", return_value=printer):
            out = _tool("run_quick_print")(model_path=model, material="PETG", skip_validation=True)
        assert out["success"] is False and printer.started == []
        assert out["code"] == "AMS_MATERIAL_NOT_LOADED"
        step = next(s for s in out["steps"] if s["name"] == "start_print")
        assert step["message"] == S3
        load, reslice = out["options"]
        assert load["action"] == "load_spool" and load["material"] == "PETG"
        assert reslice["call"] == {"tool": "run_reslice_and_print", "arguments": {
            "model_path": model, "material": "pla", "ams_mapping": [0]}}


# ---------------------------------------------------------------------------
# The doors that pick spools before they slice
# ---------------------------------------------------------------------------


class _Doors:
    def __init__(self, reading: dict[str, Any]) -> None:
        self.adapter = mock.MagicMock()
        self.adapter.get_ams_status.return_value = reading
        self.reading = reading
        self.calls: list[dict[str, Any]] = []

    def run(self, name: str, **kwargs: Any) -> dict[str, Any]:
        import kiln.server as srv

        with mock.patch.object(srv, "_resolve_adapter", return_value=self.adapter), \
                mock.patch.object(srv, "_get_adapter", return_value=self.adapter), \
                mock.patch.object(srv, "ams_status", return_value={"success": True, **self.reading}), \
                mock.patch.object(srv, "_refuse_undriven_multi_material", return_value=None), \
                mock.patch.object(srv, "run_reslice_and_print",
                                  side_effect=lambda **kw: self.calls.append(kw) or {"success": True}):
            return _tool(name)(**kwargs)


class TestSmartReprint:
    def test_the_right_colour_in_the_wrong_plastic_before_slicing(self, tmp_path):
        model = _stl(tmp_path / "part.stl")
        dc.record(model, dc.ShownColour("red", "#FF0000"))
        doors = _Doors(FOUR_PLA)
        out = doors.run("smart_reprint", file_name=model, material_id="petg")
        assert out["success"] is False and doors.calls == []
        assert out["error"]["code"] == "AMS_MATERIAL_NOT_LOADED"
        assert out["error"]["message"] == S1.replace(
            "the file is sliced for PETG's temperatures", "this print is set up for PETG's temperatures",
        ).replace("re-slice it for PLA and print from A4?", "slice it for PLA and print from A4 instead?")
        reslice, load = out["options"]
        assert reslice["call"] == {"tool": "smart_reprint", "arguments": {"file_name": model, "material_id": "pla"}}
        assert load["action"] == "load_spool" and load["material"] == "petg"

    def test_the_right_plastic_in_the_wrong_colour_prints_from_the_slot_chosen(self, tmp_path):
        model = _stl(tmp_path / "part.stl")
        dc.record(model, dc.ShownColour("red", "#FF0000"))
        doors = _Doors(NO_RED)
        out = doors.run("smart_reprint", file_name=model, material_id="pla")
        assert out["error"]["code"] == "COLOR_NOT_LOADED" and out["error"]["message"] == S2
        orange = _by(out["options"], "print_from_slot")[0]
        assert orange["call"] == {"tool": "reprint_with_material", "arguments": {
            "file_path": model, "material_id": "pla", "ams_mapping": [2]}}


class TestMultiMaterialPrint:
    def test_one_piece_missing(self, tmp_path):
        objects = [{"file_path": _stl(tmp_path / f"{c}.stl"), "material_id": "pla", "color": h}
                   for c, h in (("w", "#FFFFFF"), ("k", "#111111"), ("r", "#FF0000"), ("b", "#0000FF"))]
        doors = _Doors(NO_BLUE)
        out = doors.run("multi_material_print", objects_json=json.dumps(objects))
        assert out["success"] is False and doors.calls == []
        assert out["error"]["code"] == "AMS_COLOR_MISMATCH"
        assert out["error"]["message"] == S4
        (load,) = out["options"]
        assert (load["part"], load["material"], load["color"], load["empty_slots"]) == (3, "pla", "blue", ["A4"])

    def test_a_colour_kiln_picked_is_not_held_to(self, tmp_path):
        doors = _Doors(_ams(("PLA", GREY), ("PLA", BLUE)))
        doors.run("multi_material_print", objects_json=json.dumps([
            {"file_path": _stl(tmp_path / "a.stl"), "material_id": "pla"},
            {"file_path": _stl(tmp_path / "b.stl"), "material_id": "pla", "color": "#0000FF"}]))
        assert len(doors.calls) == 1 and doors.calls[0]["ams_mapping"] == "[0, 1]"

    def test_a_substitute_rewrites_only_that_piece(self, tmp_path):
        objects = [{"file_path": _stl(tmp_path / "w.stl"), "material_id": "pla", "color": "#FFFFFF"},
                   {"file_path": _stl(tmp_path / "b.stl"), "material_id": "pla", "color": "#0000FF"}]
        doors = _Doors(_ams(("PLA", WHITE), ("PLA", "00A000FF")))
        out = doors.run("multi_material_print", objects_json=json.dumps(objects))
        green = _by(out["options"], "print_from_slot")[0]
        assert green["call"]["tool"] == "multi_material_print"
        assert [o["color"] for o in green["call"]["arguments"]["objects_json"]] == ["#FFFFFF", "#00A000"]


class TestMultiColorCopies:
    def test_one_copy_missing(self, tmp_path):
        model = _stl(tmp_path / "part.stl")
        doors = _Doors(_ams(("PLA", WHITE), ("PLA", "00A000FF")))
        out = doors.run("multi_color_copies", model_path=model, copies=2, colors=["#FFFFFF", "#0000FF"])
        assert out["success"] is False and doors.calls == []
        assert out["error"]["message"] == (
            "One of the two colours is loaded. The blue copy needs blue PLA, and there's none loaded. "
            "Load blue in place of a spool this print doesn't use, or pick a loaded colour for that copy."
        )
        green = _by(out["options"], "print_from_slot")[0]
        assert green["call"] == {"tool": "multi_color_copies", "arguments": {
            "model_path": model, "copies": 2, "material": "PLA", "ams_slots": [0, 1]}}

    def test_the_plastic_not_loaded(self, tmp_path):
        model = _stl(tmp_path / "part.stl")
        doors = _Doors(FOUR_PLA)
        out = doors.run("multi_color_copies", model_path=model, material="PETG")
        assert out["error"]["code"] == "NO_MATERIAL" and doors.calls == []
        assert out["error"]["message"] == S3.replace(
            "re-slice it for PLA,", "slice it for PLA instead,",
        )
        reslice = _by(out["options"], "reslice_for")[0]
        assert reslice["call"] == {"tool": "multi_color_copies", "arguments": {"model_path": model, "material": "pla"}}


# ---------------------------------------------------------------------------
# The saved goal
# ---------------------------------------------------------------------------


class TestTheGoal:
    def _resolve(self, reading: dict[str, Any], **kw: Any) -> dict[str, Any]:
        from kiln.server import _resolve_use_ams

        adapter = mock.MagicMock()
        adapter.get_ams_status.return_value = reading
        return _resolve_use_ams("auto", None, adapter, material="PETG", **kw)

    def test_a_loaded_plastic_the_goal_rules_out_is_said_and_load_comes_first(self, monkeypatch):
        import kiln._pro_print_fix_bridge as bridge

        asked: list[tuple] = []
        monkeypatch.setattr(bridge, "stand_in_fit", lambda *a, **k: asked.append((a, k)) or {
            "verdicts": {"PLA": {"suits": False, "why": "PLA would soften where this part lives"}}})
        out = self._resolve(FOUR_PLA, goal={"version_id": "v-dash"})
        assert asked[0][0] == ("PETG", ["PLA"]) and asked[0][1]["version_id"] == "v-dash"
        assert out["warnings"] == [
            "This print needs PETG, and none of your spools are PETG (you have white, black, grey and red PLA). "
            "PLA would soften where this part lives, so I'd load a PETG spool and start it as is."
        ]
        load, reslice = out["options"]
        assert load["action"] == "load_spool"
        assert reslice["suits_goal"] is False and reslice["why"] == "PLA would soften where this part lives"

    def test_a_loaded_plastic_the_goal_allows_drops_the_hedge(self, monkeypatch):
        import kiln._pro_print_fix_bridge as bridge

        monkeypatch.setattr(bridge, "stand_in_fit", lambda *a, **k: {"verdicts": {"PLA": {"suits": True, "why": None}}})
        out = self._resolve(FOUR_PLA, goal={"goal_id": "b-shelf"})
        assert out["warnings"][0].endswith("Load a PETG spool and I'll start it. Or I can re-slice it for PLA.")
        assert out["options"][1]["suits_goal"] is True

    def test_no_goal_asks_nothing_and_keeps_the_hedge(self, monkeypatch):
        import kiln.server as srv

        monkeypatch.setattr(srv, "_pro_api_call", mock.MagicMock(side_effect=AssertionError("asked")))
        out = self._resolve(FOUR_PLA)
        assert out["warnings"] == [S3]

    def test_a_goal_check_that_did_not_answer_keeps_the_hedge_and_says_why_beside_it(self, monkeypatch):
        import kiln._pro_print_fix_bridge as bridge
        import kiln.server as srv

        monkeypatch.setattr(bridge, "_local_stand_ins", lambda *a: bridge._NOT_HERE)
        monkeypatch.setattr(bridge, "_plan_note", lambda m: None)
        monkeypatch.setattr(srv, "_pro_api_call", mock.MagicMock(side_effect=OSError("offline")))
        out = self._resolve(FOUR_PLA, goal={"version_id": "v-1"})
        assert out["warnings"] == [S3]
        missed = out["options"][1]["goal_check_missed"]
        assert missed.startswith("Whether a loaded filament suits what this part is for is checked by Kiln's servers.")

    def test_a_plan_below_kiln_pro_asks_nothing(self, monkeypatch):
        import kiln._pro_print_fix_bridge as bridge
        import kiln.server as srv

        monkeypatch.setattr(bridge, "_local_stand_ins", lambda *a: bridge._NOT_HERE)
        monkeypatch.setattr(bridge, "_plan_note", lambda m: "Kiln can also ... Your plan is Free.")
        monkeypatch.setattr(srv, "_pro_api_call", mock.MagicMock(side_effect=AssertionError("asked")))
        out = self._resolve(FOUR_PLA, goal={"version_id": "v-1"})
        assert out["warnings"] == [S3] and "goal_check_missed" not in out["options"][1]

    def test_the_served_answer_is_carried_as_said(self, monkeypatch):
        import kiln._pro_print_fix_bridge as bridge
        import kiln.server as srv

        monkeypatch.setattr(bridge, "_local_stand_ins", lambda *a: bridge._NOT_HERE)
        monkeypatch.setattr(bridge, "_plan_note", lambda m: None)
        sent: dict[str, Any] = {}
        monkeypatch.setattr(srv, "_pro_api_call", lambda tool, **kw: sent.update(tool=tool, **kw) or {
            "success": True, "status": "ok", "known": True,
            "stand_ins": [{"material": "PLA", "suits": False, "why": "PLA isn't food-safe for what this part is for"}]})
        out = self._resolve(FOUR_PLA, goal={"version_id": "v-mug"})
        assert sent["tool"] == "material_pick" and sent["blocked_material"] == "PETG"
        assert sent["stand_ins"] == ["PLA"] and sent["version_id"] == "v-mug" and sent["_asked_by_user"] is False
        assert "PLA isn't food-safe for what this part is for, so I'd load a PETG spool" in out["warnings"][0]


# ---------------------------------------------------------------------------
# What stays as it was
# ---------------------------------------------------------------------------


class TestSafetyFirst:
    def test_an_abrasive_job_is_sliced_first_so_its_own_refusal_can_speak(self, tmp_path):
        doors = _Doors(FOUR_PLA)
        doors.run("smart_reprint", file_name=_stl(tmp_path / "part.stl"), material_id="petg_cf")
        assert len(doors.calls) == 1 and doors.calls[0]["material"] == "petg_cf"


class TestPlainWords:
    @pytest.mark.parametrize("words", ["AMS mapping", "ams_mapping", "tray_id", "tray_type", "Not started"])
    def test_no_internal_words_in_any_sentence(self, words):
        from kiln.ams_routing import Filament, Tray, plan_ams_mapping, spool_miss

        trays = [Tray(i, "PLA", h, name=f"A{i + 1}") for i, h in enumerate(("FFFFFF", "111111", "808080", "FF0000"))]
        said = [
            spool_miss([Filament("FF0000", "PETG")], trays, names=["red"]).message,
            spool_miss([Filament("0000FF", "PLA")], trays[:3], names=["blue"]).message,
            spool_miss([Filament(None, "PETG")], trays, colour_counts=False).message,
            spool_miss([Filament(h, "PLA") for h in ("FFFFFF", "111111", "FF0000", "0000FF")], trays[:3] + [trays[3]],
                       plan=plan_ams_mapping([Filament(h, "PLA") for h in ("FFFFFF", "111111", "FF0000", "0000FF")],
                                             trays)).message,
        ]
        assert not any(words in s for s in said)
