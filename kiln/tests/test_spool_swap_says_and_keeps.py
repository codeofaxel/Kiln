"""Which spool a file prints from is said, a swap's stakes are said by what
the part is for, and a re-slice taken from a choice lands on the part's history.

Three things, through the registered doors with fake trays:

* a file that names only its own plastic, with nothing named by the person,
  prints from the first spool loaded and says so: in the approval line before
  the start, and in the result;
* a re-slice for another plastic is offered by what the part's saved goal
  says it is for, as Kiln's servers answer it: a swap that would be unsafe is
  steered away from at every plan; a swap that would only be worse is said on
  Kiln Pro and hedged, with one line about the plan, below it; with nothing
  to check against, everyone gets the hedge and the reason sits beside it;
* the re-slice choice carries the part's identity and the plastic it stands
  in for, so the print it starts is recorded on that version, in the plastic
  it really printed in, with the one it replaced.
"""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from kiln import declared_color as dc
from tests.test_spool_miss_says_and_offers import FOUR_PLA, _ams, _gcode, _Printer, _tool

WHITE, RED = "FFFFFFFF", "FF0000FF"
FIRST_PLA = _ams(("PLA", WHITE), ("PETG", "111111FF"))
FIRST_PETG = _ams(("PETG", "111111FF"), ("PLA", WHITE))
TPU_ONLY = _ams(("TPU", WHITE))

LINE_SAME = "Printing from slot A1, white PLA: the first spool loaded."
LINE_OTHER = (
    "This file was made for PLA, but no plastic was named, so it would print from the first spool loaded: "
    "slot A1, which holds PETG. If that's not what you want, cancel and tell me which slot to use."
)


@pytest.fixture(autouse=True)
def _gates(monkeypatch):
    monkeypatch.setenv("KILN_SKIP_PREVIEW_GATE", "1")
    monkeypatch.setattr("kiln.server._check_auth", lambda *a, **k: None)
    monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)


def _start(printer: Any, file_name: str, **kw: Any) -> dict[str, Any]:
    import kiln.server as srv

    with mock.patch.object(srv, "_get_adapter", return_value=printer), \
            mock.patch.object(srv, "_resolve_adapter", return_value=printer), \
            mock.patch.object(srv, "_check_rate_limit", return_value=None), \
            mock.patch.object(srv, "preflight_check", return_value={"ready": True}):
        return _tool("start_print")(file_name, **kw)


def _plain_gcode(path: Path, kind: str) -> str:
    path.write_text(f"G28\nG1 X1 Y1\n; filament_type = {kind}\n")
    return str(path)


# ---------------------------------------------------------------------------
# Which spool a file prints from
# ---------------------------------------------------------------------------


class TestTheFirstSpoolIsSaid:
    def test_the_first_spool_is_the_file_s_plastic(self, tmp_path):
        part = _plain_gcode(tmp_path / "part.gcode", "PLA")
        printer = _Printer(FIRST_PLA, (part,))
        out = _start(printer, part)
        assert out["success"] is True, out
        assert printer.started[0][1]["ams_mapping"] == [0]
        assert out["ams_warnings"] == [LINE_SAME]
        # A plastic the person named picks its own spool: this line is not said.
        from kiln.server import _resolve_use_ams

        named = _resolve_use_ams("auto", None, printer, material="PLA", file_path=part)
        assert named["ams_mapping"] == [0] and LINE_SAME not in named["warnings"]

    def test_the_first_spool_is_another_plastic(self, tmp_path):
        part = _plain_gcode(tmp_path / "part.gcode", "PLA")
        printer = _Printer(FIRST_PETG, (part,))
        out = _start(printer, part)
        assert out["success"] is True, out
        assert printer.started[0][1]["ams_mapping"] == [0], "unchanged: it still prints from the first spool"
        assert out["ams_warnings"] == [LINE_OTHER]

    def test_the_approval_line_says_it_before_the_print_starts(self, tmp_path):
        import kiln.server as srv

        part = _plain_gcode(tmp_path / "part.gcode", "PLA")
        printer = _Printer(FIRST_PETG, (part,))
        with mock.patch.object(srv, "_resolve_adapter", return_value=printer), \
                mock.patch.object(srv, "_local_copy_of", return_value=part):
            said = srv._consent_filament_line("start_print", part, None, {"file_name": part})
            named = srv._consent_filament_line("start_print", part, None, {"file_name": part, "ams_mapping": [1]})
        assert said == LINE_OTHER
        assert named != LINE_OTHER, "a tray the person named is not the first spool"

# ---------------------------------------------------------------------------
# What the part is for decides what a swap is told
# ---------------------------------------------------------------------------


def _served(monkeypatch, *, rows: list[dict[str, Any]], purpose_known: bool = True, plan_line: str | None = None):
    """Kiln's servers answering the loaded-filament question as they would for
    one plan, the part's goal named by its kept version."""
    import kiln._pro_print_fix_bridge as bridge
    import kiln.server as srv

    sent: list[dict[str, Any]] = []

    def answer(tool: str, **kw: Any) -> dict[str, Any]:
        if tool != "material_pick":
            return {"status": "error", "error": "not part of this test", "why": "unanswered"}
        sent.append({"tool": tool, **kw})
        return {"success": True, "status": "ok", "known": True, "purpose_known": purpose_known, "stand_ins": rows}

    monkeypatch.setattr(bridge, "_local_stand_ins", lambda *a: bridge._NOT_HERE)
    monkeypatch.setattr(srv, "_pro_api_call", answer)
    monkeypatch.setattr(bridge, "_stand_in_plan_note", lambda: plan_line)
    return sent


def _not_loaded(tmp_path: Path, version_id: str = "v-part") -> dict[str, Any]:
    """slice_and_print of a PETG part onto a printer holding only TPU."""
    from tests.test_spool_miss_says_and_offers import TestSliceAndPrint

    out, printer, _model = TestSliceAndPrint()._run(tmp_path, TPU_ONLY, types="PETG", material="PETG",
                                                    version_id=version_id)
    assert printer.started == []
    return out


FREE_LINE = "Kiln can also check a swap like this against what your goal says the part is for. Your plan is Free."
FIRST = "This print needs PETG, and none of your spools are PETG (you have white TPU)."


class TestTheStakes:
    def test_a_swap_unsafe_for_the_part_is_steered_away_from_at_free(self, monkeypatch, tmp_path):
        sent = _served(monkeypatch, plan_line=FREE_LINE, rows=[{
            "material": "TPU", "suits": False, "offer": False,
            "why": "this part holds weight, and TPU would soften where it lives"}])
        out = _not_loaded(tmp_path)
        assert sent and sent[0]["tool"] == "material_pick" and sent[0]["version_id"] == "v-part"
        assert out["error"]["message"] == (
            f"{FIRST} Re-slicing for TPU isn't a good idea here: this part holds weight, and TPU would soften "
            "where it lives. Load a PETG spool and I'll start it."
        )
        load, reslice = out["options"]
        assert load["action"] == "load_spool"
        assert reslice["offered"] is False and reslice["suits_goal"] is False

    def test_a_swap_only_worse_at_free_is_hedged_with_one_plan_line(self, monkeypatch, tmp_path):
        _served(monkeypatch, plan_line=FREE_LINE, rows=[{"material": "TPU", "suits": None, "offer": True}])
        out = _not_loaded(tmp_path)
        assert out["error"]["message"] == (
            f"{FIRST} Load a PETG spool and I'll start it. Or I can re-slice it for TPU, if TPU suits what this "
            f"part is for. {FREE_LINE}"
        )
        assert out["error"]["message"].count("Kiln can also check") == 1

    def test_a_swap_only_worse_on_kiln_pro_is_said_and_still_offered(self, monkeypatch, tmp_path):
        _served(monkeypatch, plan_line=None, rows=[{
            "material": "TPU", "suits": False, "offer": True,
            "why": "TPU would soften and sag where this part lives; PETG holds its shape there"}])
        out = _not_loaded(tmp_path)
        assert out["error"]["message"] == (
            f"{FIRST} TPU would soften and sag where this part lives; PETG holds its shape there. "
            "Load a PETG spool, or I can re-slice it for TPU if that's fine."
        )
        reslice = out["options"][1]
        assert reslice["offered"] is True and reslice["call"]["tool"] == "reprint_with_material"

    @pytest.mark.parametrize("plan_line", [FREE_LINE, None], ids=["free", "pro"])
    def test_nothing_to_check_against_gets_the_hedge_and_the_reason_beside_it(self, monkeypatch, tmp_path, plan_line):
        import kiln._pro_print_fix_bridge as bridge

        _served(monkeypatch, plan_line=plan_line, purpose_known=False,
                rows=[{"material": "TPU", "suits": None, "offer": True}])
        out = _not_loaded(tmp_path)
        assert out["error"]["message"] == (
            f"{FIRST} Load a PETG spool and I'll start it. Or I can re-slice it for TPU, if TPU suits what this "
            "part is for."
        )
        assert out["options"][1]["goal_check_missed"] == bridge.NO_NEEDS

    def test_no_goal_gets_the_hedge_and_asks_nothing(self, monkeypatch, tmp_path):
        import kiln._pro_print_fix_bridge as bridge
        import kiln.server as srv

        asked: list[str] = []
        monkeypatch.setattr(srv, "_pro_api_call", lambda tool, **kw: asked.append(tool) or {
            "status": "error", "error": "not part of this test", "why": "unanswered"})
        out = _not_loaded(tmp_path, version_id="")
        assert "material_pick" not in asked
        assert out["error"]["message"].endswith("if TPU suits what this part is for.")
        assert out["options"][1]["goal_check_missed"] == bridge.NO_GOAL

    def test_a_guess_is_a_condition_with_one_question(self, monkeypatch, tmp_path):
        _served(monkeypatch, plan_line=None, rows=[{
            "material": "TPU", "suits": None, "offer": True,
            "why": "If this will sit close to a bulb, heater or anything else that gets hot, TPU would soften there",
            "ask": "Will this sit close to a bulb, heater or anything else that gets hot?"}])
        out = _not_loaded(tmp_path)
        said = out["error"]["message"]
        assert said.endswith(
            "Or I can re-slice it for TPU. If this will sit close to a bulb, heater or anything else that gets "
            "hot, TPU would soften there. Will this sit close to a bulb, heater or anything else that gets hot?"
        )
        assert said.count("?") == 1


# ---------------------------------------------------------------------------
# A re-slice choice lands on the part's history
# ---------------------------------------------------------------------------


def _cube(path: Path) -> str:
    v = [(0, 0, 0), (20, 0, 0), (20, 20, 0), (0, 20, 0), (0, 0, 20), (20, 0, 20), (20, 20, 20), (0, 20, 20)]
    faces = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
             (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    with open(path, "wb") as fh:
        fh.write(b"\x00" * 80 + struct.pack("<I", len(faces)))
        for f in faces:
            fh.write(struct.pack("<3f", 0, 0, 0))
            for i in f:
                fh.write(struct.pack("<3f", *v[i]))
            fh.write(struct.pack("<H", 0))
    return str(path)


@pytest.fixture
def kiln_home(tmp_path, monkeypatch):
    monkeypatch.setenv("KILN_DB_PATH", str(tmp_path / "kiln.db"))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("KILN_HOME", str(tmp_path / ".kiln"))
    import kiln.persistence as _p

    monkeypatch.setattr(_p, "_db", None, raising=False)
    yield tmp_path
    monkeypatch.setattr(_p, "_db", None, raising=False)


def _real_printer(reading: dict[str, Any], files: tuple[str, ...]) -> Any:
    """A printer through the start template every print passes through, so
    the print's record is opened as it is for a real machine."""
    from kiln.printers.base import PrinterAdapter, PrinterFile, PrinterState, PrinterStatus, PrintResult, UploadResult

    class _Fake(PrinterAdapter):
        def __init__(self) -> None:
            self._kiln_registered_name = "bench"
            self.started: list[tuple[str, dict[str, Any]]] = []
            self.files = list(files)

        def _start_print_impl(self, file_name: str, **kwargs: Any) -> PrintResult:
            self.started.append((file_name, kwargs))
            return PrintResult(success=True, message="started")

        def get_ams_status(self) -> dict[str, Any]:
            return reading

        def get_state(self):
            return PrinterState(connected=True, state=PrinterStatus.IDLE, tool_temp_actual=22.0,
                                tool_temp_target=0.0, bed_temp_actual=21.0, bed_temp_target=0.0)

        def get_job(self):
            return mock.MagicMock(file_name=None)

        def list_files(self):
            return [PrinterFile(name=Path(f).name, path=f, size_bytes=100) for f in self.files]

        def upload_file(self, file_path):
            self.files.append(file_path)
            return UploadResult(success=True, file_name=Path(file_path).name, message="uploaded")

        def connect(self): ...
        def disconnect(self): ...
        def get_printer_info(self): ...
        def cancel_print(self): ...
        def pause_print(self): ...
        def resume_print(self): ...

    _Fake.name = "bench"
    _Fake.__abstractmethods__ = frozenset()
    return _Fake()


class TestTheChoiceLandsOnThePartsHistory:
    def test_the_third_choice_of_a_colour_miss_carries_the_part_and_its_plastic(self, monkeypatch, tmp_path):
        from tests.test_spool_miss_says_and_offers import ORANGE, TestSliceAndPrint

        _served(monkeypatch, plan_line=None, rows=[{"material": "PETG", "suits": True, "why": None, "offer": True}])
        reading = _ams(("PLA", WHITE), ("PLA", ORANGE), ("PETG", RED))
        out, printer, model = TestSliceAndPrint()._run(tmp_path, reading, types="PLA", asked="red", material="PLA",
                                                       version_id="v-part", branch_id="b-main")
        assert out["error"]["code"] == "COLOR_NOT_LOADED" and printer.started == []
        assert "re-slice it for the red PETG in slot A3" in out["error"]["message"]
        third = next(o for o in out["options"] if o["action"] == "reslice_for")
        assert third["call"] == {"tool": "reprint_with_material", "arguments": {
            "file_path": model, "material_id": "petg", "ams_mapping": [2], "version_id": "v-part",
            "branch_id": "b-main", "in_place_of": "PLA", "chosen_by": "person"}}

    def test_a_re_slice_taken_from_start_print_is_kept_on_the_version_in_its_real_plastic(self, kiln_home):
        import kiln.server as srv
        from kiln.persistence import get_db
        from kiln.print_lineage import resolve
        from kiln.slicer import SliceResult, _record_slice

        model = _cube(kiln_home / "bracket.stl")
        first = _gcode(kiln_home / "bracket.gcode", "#FF0000", "PETG")
        _record_slice(None, model, first)
        dc.record(first, dc.ShownColour("red", "#FF0000"))
        printer = _real_printer(FOUR_PLA, (first,))
        ids = {"version_id": "v-bracket-3", "design_id": "d-bracket", "branch_id": "b-main"}

        out = _start(printer, first, **ids)
        assert out["error"]["code"] == "AMS_MATERIAL_NOT_LOADED" and printer.started == []
        reslice = next(o for o in out["options"] if o["action"] == "reslice_for")
        call = reslice["call"]
        assert call["tool"] == "reprint_with_material" and "needs" not in call
        assert call["arguments"] == {"file_path": model, "material_id": "pla", "ams_mapping": [3],
                                     **ids, "in_place_of": "PETG", "chosen_by": "person"}

        sliced = kiln_home / "out" / "bracket_pla.gcode"
        sliced.parent.mkdir()

        def fake_slice_file(path, **_kw):
            _gcode(sliced, "#FF0000", "PLA")
            _record_slice(None, path, str(sliced))
            return SliceResult(success=True, output_path=str(sliced), slicer="prusa-slicer", message="ok")

        with mock.patch("kiln.pipelines._resolve_pipeline_adapter", return_value=printer), \
                mock.patch.object(srv, "_resolve_adapter", return_value=printer), \
                mock.patch("kiln.slicer.slice_file", side_effect=fake_slice_file), \
                mock.patch("kiln.pipelines._safety_check_step",
                           return_value=mock.MagicMock(success=True, name="safety_check", data={}, message="ok")):
            done = _tool(call["tool"])(**call["arguments"])
        assert done.get("success") is True, done
        assert len(printer.started) == 1

        # The version's print history, read off the row the start opened.
        rows = get_db().list_print_outcomes(version_ids=["v-bracket-3"], include_all=True)
        assert len(rows) == 1, rows
        row = rows[0]
        assert (row["version_id"], row["design_id"], row["branch_id"]) == ("v-bracket-3", "d-bracket", "b-main")
        assert str(row["material_type"]).upper() == "PLA"
        part = resolve(printer.started[0][0], local_paths=[str(sliced)])
        assert part["material_swap"] == {"asked": "PETG", "used": "pla"}
        assert part["stated"].get("material", "PLA").upper().startswith("PLA")
