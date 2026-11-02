"""A saved goal learns the filament its part really printed in.

A goal kept by ``design_session`` records what a part is for and its
filament, and the next make and every check from the goal start from that
filament.  When the part printed in another one -- a reprint in another
material, a swap after Kiln refused a filament the printer cannot melt --
nothing told the goal, so the next make started again from the filament the
person had set aside.

The start template every door passes through now tells the goal, through the
served ``design_session`` update step.  These tests start prints on a REAL
adapter (the template is inherited, never mocked) and stand in only for the
servers' answer: the door names the goal or the model's own note does, the
filament comes from the start or from the sliced file, and a miss is said
once.  The other half -- the goal moving the new filament to the front and
keeping the old one with its reason -- is kiln-pro's, and is proven there
through this same door.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

import pytest

from kiln import server
from kiln.printers import base
from kiln.printers.base import (
    JobProgress,
    PrinterAdapter,
    PrinterCapabilities,
    PrinterFile,
    PrinterState,
    PrinterStatus,
    PrintResult,
    UploadResult,
)

GOAL = "0123456789abcdef0123456789abcdef"


class _Bench(PrinterAdapter):
    """A real adapter that talks to nothing: the start template is inherited."""

    def __init__(self, model: str = "bambu_a1") -> None:
        self.host = "192.0.2.20"
        self.serial = ""
        self._model = model
        self._status = PrinterStatus.IDLE
        self.started: list[tuple[str, dict[str, Any]]] = []

    @property
    def name(self) -> str:
        return "bench"

    @property
    def capabilities(self) -> PrinterCapabilities:
        return PrinterCapabilities()

    def declared_printer_model(self) -> str | None:
        return self._model

    def get_state(self) -> PrinterState:
        return PrinterState(
            connected=True, state=self._status, tool_temp_actual=25.0, tool_temp_target=0.0,
            bed_temp_actual=25.0, bed_temp_target=0.0,
        )

    def get_job(self) -> JobProgress:
        return JobProgress(file_name=self.started[-1][0] if self.started else None)

    def list_files(self) -> list[PrinterFile]:
        return []

    def upload_file(self, file_path: str) -> UploadResult:
        return UploadResult(success=True, file_name=os.path.basename(file_path), message="uploaded")

    def delete_file(self, file_name: str) -> bool:
        return True

    def _start_print_impl(self, file_name: str, **kwargs: Any) -> PrintResult:
        self.started.append((file_name, kwargs))
        self._status = PrinterStatus.PRINTING
        return PrintResult(success=True, message="started")

    def cancel_print(self) -> PrintResult:
        return PrintResult(success=True, message="cancelled")

    def pause_print(self) -> PrintResult:
        return PrintResult(success=True, message="paused")

    def _resume_print_impl(self) -> PrintResult:
        return PrintResult(success=True, message="resumed")

    def emergency_stop(self) -> PrintResult:
        return PrintResult(success=True, message="stopped")

    def send_gcode(self, commands: Any) -> bool:
        return True

    def set_tool_temp(self, celsius: float, tool: int = 0) -> bool:
        return True

    def set_bed_temp(self, celsius: float) -> bool:
        return True

    def _load_filament_impl(self, plan: Any) -> Any:
        raise NotImplementedError

    def _unload_filament_impl(self, plan: Any) -> Any:
        raise NotImplementedError

    def _purge_filament_impl(self, plan: Any) -> Any:
        raise NotImplementedError


class _Servers:
    """Stands in for Kiln's servers' answer to ``design_session``; every
    call to it is kept so a test reads exactly what the install sent.  Any
    other ask a door makes of the servers on the way (on an install without
    kiln-pro, the reprint's melt check) is not the goal's, and gets no
    answer."""

    def __init__(self, answer: dict[str, Any] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.answer = answer

    def __call__(self, tool: str, /, **kwargs: Any) -> dict[str, Any]:
        if tool != "design_session":
            return {"status": "error", "code": "SERVER_UNREACHABLE", "error": "not asked in this test"}
        self.calls.append((tool, kwargs))
        if self.answer is not None:
            return self.answer
        change = kwargs.get("material_change") or {}
        return {
            "status": "success",
            "material_change": {
                "brief_id": kwargs.get("goal_id"),
                "recorded": True,
                "material": str(change.get("material", "")).upper(),
                "replaced": "PEEK",
                "why": "the printer cannot melt it",
                "note": "The goal now records it.",
            },
        }


@pytest.fixture(autouse=True)
def _quiet_start(monkeypatch):
    monkeypatch.setattr(base, "_PRINT_STARTED_HOOKS", (), raising=False)
    monkeypatch.setattr(base, "_preflight_upload_or_raise", lambda adapter, path: None, raising=False)
    monkeypatch.setattr(server, "_check_auth", lambda *_a, **_k: None)
    monkeypatch.setenv("KILN_SKIP_PREVIEW_GATE", "1")
    monkeypatch.setenv("KILN_SKIP_PREFLIGHT", "1")


@pytest.fixture
def servers(monkeypatch) -> _Servers:
    stand_in = _Servers()
    monkeypatch.setattr(server, "_served_bearer", lambda: ("token", None))
    monkeypatch.setattr(server, "_pro_api_call", stand_in)
    return stand_in


def _sliced(tmp_path: Path, *, types: str = "PETG-CF", goal: str | None = GOAL) -> tuple[Path, Path]:
    """A model, its goal note when *goal* is given, and a G-code sliced from
    it for *types*, recorded in the slice ledger the way the slicer does."""
    from kiln.monitor_twin import note_sliced

    model = tmp_path / "dash_mount.stl"
    model.write_text("solid m\nendsolid m\n")
    if goal is not None:
        (tmp_path / "dash_mount.stl.intent.json").write_text(
            json.dumps({"intent_id": "i1", "generator": f"design_brief:{goal}", "assertions": []})
        )
    gcode = tmp_path / "dash_mount.gcode"
    gcode.write_text(f"; generated by PrusaSlicer\nG28\nG1 X1\n; filament_type = {types}\n")
    note_sliced(str(model), str(gcode))
    return model, gcode


def _wait_for_the_note() -> None:
    for thread in threading.enumerate():
        if thread.name == "kiln-goal-filament":
            thread.join(10)


def _reprint_door(bench: _Bench, *, start_kwargs: dict[str, Any] | None = None):
    """``run_reslice_and_print`` as the pipeline runs it: the start is the
    adapter's own template, with the arguments the pipeline passes."""

    def run(**kwargs: Any) -> dict[str, Any]:
        result = bench.start_print(os.path.basename(kwargs["_gcode"]), **(start_kwargs or {}))
        return {"success": result.success, "message": result.message}

    return run


def _reprint(monkeypatch, bench: _Bench, gcode: Path, **door_kwargs: Any) -> dict[str, Any]:
    run = _reprint_door(bench)
    monkeypatch.setattr(server, "run_reslice_and_print", lambda **kw: run(_gcode=str(gcode), **kw))
    tool = getattr(server.reprint_with_material, "fn", server.reprint_with_material)
    return tool(file_path=str(gcode.with_suffix(".stl")), material_id="petg_cf", printer_id="bambu_a1", **door_kwargs)


# ---------------------------------------------------------------------------
# The reprint door
# ---------------------------------------------------------------------------


class TestTheReprintDoor:
    def test_the_goal_is_told_the_material_and_the_reply_says_what_it_recorded(
        self, tmp_path, monkeypatch, servers,
    ):
        _model, gcode = _sliced(tmp_path, goal=None)
        result = _reprint(monkeypatch, _Bench(), gcode, brief_id=GOAL, chosen_by="person")

        assert result["success"] is True
        assert len(servers.calls) == 1
        tool, sent = servers.calls[0]
        assert tool == "design_session"
        assert sent["verb"] == "update"
        assert sent["goal_id"] == GOAL
        assert sent["material_change"] == {
            "material": "PETG-CF",
            "via": "reprint_with_material",
            "decided_by": "person",
            "printer_model": "bambu_a1",
        }
        said = result["goal_material"]
        assert said["recorded"] is True
        assert said["replaced"] == "PEEK"
        assert said["recorded_on"] == "kiln_servers"

    def test_the_goal_beside_the_model_is_found_when_the_door_names_none(
        self, tmp_path, monkeypatch, servers,
    ):
        _model, gcode = _sliced(tmp_path)
        result = _reprint(monkeypatch, _Bench(), gcode)

        assert [call[1]["goal_id"] for call in servers.calls] == [GOAL]
        assert "decided_by" not in servers.calls[0][1]["material_change"]
        assert result["goal_material"]["recorded"] is True

    def test_a_signed_out_install_says_the_goal_was_not_told_once(self, tmp_path, monkeypatch, servers):
        monkeypatch.setattr(server, "_served_bearer", lambda: ("", None))
        _model, gcode = _sliced(tmp_path)
        result = _reprint(monkeypatch, _Bench(), gcode)

        said = result["goal_material"]
        assert said["recorded"] is False
        assert said["why"] == "signed_out"
        assert said["note"].count("Kiln can't record that this part printed in PETG-CF") == 1
        assert "The print runs as started" in said["note"]
        assert servers.calls == []
        # The print itself stands.
        assert result["success"] is True

    def test_a_goal_the_account_does_not_hold_is_said_by_name(self, tmp_path, monkeypatch, servers):
        servers.answer = {"status": "error", "code": "NOT_FOUND", "error": "no such goal"}
        _model, gcode = _sliced(tmp_path)
        said = _reprint(monkeypatch, _Bench(), gcode)["goal_material"]
        assert said["recorded"] is False
        assert GOAL in said["note"] and "not in your Kiln account" in said["note"]


# ---------------------------------------------------------------------------
# Every other door: the start template alone
# ---------------------------------------------------------------------------


class TestEveryOtherDoor:
    def test_a_print_of_a_goals_part_tells_the_goal_with_no_door_in_front(self, tmp_path, servers):
        _model, gcode = _sliced(tmp_path)
        _Bench().start_print(gcode.name)
        _wait_for_the_note()

        assert len(servers.calls) == 1
        sent = servers.calls[0][1]
        assert sent["goal_id"] == GOAL
        assert sent["material_change"]["material"] == "PETG-CF"
        assert sent["material_change"]["via"] == "print"

    def test_the_material_the_start_commanded_outranks_the_files_label(self, tmp_path, servers):
        _model, gcode = _sliced(tmp_path, types="PLA")
        _Bench().start_print(gcode.name, material="ASA")
        _wait_for_the_note()
        assert servers.calls[0][1]["material_change"]["material"] == "ASA"

    def test_a_print_of_no_goals_part_sends_nothing(self, tmp_path, servers):
        _model, gcode = _sliced(tmp_path, goal=None)
        _Bench().start_print(gcode.name)
        _wait_for_the_note()
        assert servers.calls == []

    def test_a_file_sliced_for_several_filaments_names_none(self, tmp_path, servers):
        _model, gcode = _sliced(tmp_path, types="PLA;PETG")
        _Bench().start_print(gcode.name)
        _wait_for_the_note()
        assert servers.calls == []

    def test_a_refused_start_tells_the_goal_nothing(self, tmp_path, servers, monkeypatch):
        _model, gcode = _sliced(tmp_path)
        bench = _Bench()
        monkeypatch.setattr(bench, "_start_print_impl", lambda *_a, **_k: PrintResult(success=False, message="no"))
        bench.start_print(gcode.name)
        _wait_for_the_note()
        assert servers.calls == []


# ---------------------------------------------------------------------------
# One spelling of a material
# ---------------------------------------------------------------------------


class TestSpelling:
    @pytest.mark.parametrize(
        ("a", "b"),
        [("PETG-CF", "petg_cf"), ("cf petg", "PETG CF"), ("PA-CF", "cf_nylon"), ("PLA+", "pla_plus")],
    )
    def test_one_material_however_it_is_spelled(self, a, b):
        from kiln.catalog_keys import same_material_spelling

        assert same_material_spelling(a, b)

    @pytest.mark.parametrize(("a", "b"), [("PETG", "PETG-CF"), ("PLA", ""), ("", "")])
    def test_a_family_or_a_blank_is_not_the_same_material(self, a, b):
        from kiln.catalog_keys import same_material_spelling

        assert not same_material_spelling(a, b)

    def test_a_catalog_id_reads_the_way_a_goal_keeps_it(self):
        from kiln.catalog_keys import material_label

        assert material_label("petg_cf") == "PETG-CF"
        assert material_label("pla_plus") == "PLA+"
        assert material_label("  Bambu  PLA Basic ") == "BAMBU PLA BASIC"
        assert material_label("") == ""
