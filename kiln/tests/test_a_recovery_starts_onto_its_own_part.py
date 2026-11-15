"""A recovery Kiln made for the print still on the plate starts onto that part,
with the person's yes to that print; nothing else starts onto a part.

After a cancel or a power loss the printer is not paused, so continuing the
print is a new print: the person is asked, it is previewed, the safety gate
runs.  The plate record still says a part is there, and every start onto an
occupied plate is held.  The one start that passes the hold is a resume file
Kiln built from its own copy of the print recorded on that plate, with no
other part beside it, while the person says yes to this print itself.  Any
other file, a recovery of a different print, and a recovery started on a
standing yes are held; the last one with a look at the plate.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiln import monitor_twin, plate_state, print_signoff, resume_provenance, server
from kiln.print_consent import SOURCE_ELICITED, SOURCE_WINDOW
from kiln.printers import base as printers_base
from kiln.printers.base import JobProgress, PrinterAdapter, PrinterState, PrinterStatus, PrintResult, UploadResult
from tests._kiln_resume import reset_resume_records

JOB = "vase.gcode.3mf"
TOOLPATH = b"G28\nG1 Z0.2 F600\n; layer 40 of the vase\n"
OTHER = b"G28\nG1 Z0 F9000\n; not the vase\n"


class _Machine(PrinterAdapter):
    """A printer stopped after a cancel or a power loss: idle, its job gone.
    It reports a serial, so its plate has a record of its own."""

    name = "kilntest"
    serial = "KILNTEST0001"

    def __init__(self) -> None:
        self.status = PrinterStatus.IDLE
        self.job: str | None = None
        self.files: dict[str, bytes] = {}

    def get_state(self) -> PrinterState:
        return PrinterState(state=self.status, connected=True)

    def get_job(self) -> JobProgress:
        return JobProgress(file_name=self.job)

    def upload_file(self, file_path: str) -> UploadResult:
        name = os.path.basename(file_path)
        self.files[name] = Path(file_path).read_bytes()
        return UploadResult(success=True, file_name=name, message="ok")

    def _start_print_impl(self, file_name: str, **kwargs) -> PrintResult:
        return PrintResult(success=True, message="started")


_Machine.__abstractmethods__ = frozenset()


@pytest.fixture()
def machine(monkeypatch, tmp_path):
    printer = _Machine()
    printer._kiln_registered_name = "garage"
    monkeypatch.setattr(server, "_resolve_adapter", lambda *_a, **_k: printer)
    monkeypatch.setattr(printers_base, "_preflight_upload_or_raise", lambda _a, path: path)
    monkeypatch.setattr(printers_base, "_incomplete_upload_reason", lambda _a, _path: None)
    monkeypatch.setattr(monitor_twin, "_TWIN_DIR", tmp_path / "twin")
    monkeypatch.setattr(monitor_twin, "_ACTIVE_FILE", tmp_path / "twin" / "active.json")
    monkeypatch.setenv("KILN_HOME", str(tmp_path / "kiln-home"))
    monkeypatch.delenv("KILN_SKIP_PREVIEW_GATE", raising=False)
    reset_resume_records()
    print_signoff.clear()
    yield printer
    reset_resume_records()
    print_signoff.clear()


def _file(tmp_path: Path, name: str, body: bytes) -> str:
    path = tmp_path / name
    path.write_bytes(body)
    return str(path)


def _kiln_printed(machine: _Machine, tmp_path: Path, job: str = JOB) -> str:
    """Kiln started *job* on *machine*: the plate record holds its part, and
    Kiln kept its toolpath with its hash.  Returns the kept toolpath."""
    from kiln.registry import machine_fingerprint

    kept = _file(tmp_path, f"kept-{job}.gcode", TOOLPATH)
    (tmp_path / "twin").mkdir(exist_ok=True)
    active = monitor_twin._read_json(monitor_twin._ACTIVE_FILE, {}) or {}
    active[job] = {
        "file_name": job, "printer_name": "garage", "machine": machine_fingerprint(machine),
        "started_at": "2026-01-01T00:00:00+00:00", "gcode": kept,
        "gcode_sha256": hashlib.sha256(TOOLPATH).hexdigest(), "mesh": None,
    }
    monitor_twin._write_json(monitor_twin._ACTIVE_FILE, active)
    plate_state.mark_occupied(machine, plate_state.PlateJob(file=job), source="kiln_started_print")
    return kept


def _recovery(machine: _Machine, tmp_path: Path, source: str, name: str = "vase_resume_from_40.gcode") -> str:
    """What a recovery maker does: build the file, offer it to the record
    naming its source, and upload it through Kiln."""
    built = _file(tmp_path, name, b"a recovery from layer 40")
    resume_provenance.try_record_resume(built, source=source, adapter=machine, made_by="recover_power_loss_print")
    return machine.upload_file(built).file_name


def _said_yes(name: str, source: str = SOURCE_ELICITED) -> None:
    """The clearance this call carries, as the gate grants it."""
    print_signoff.grant("start_print", name, "garage", source=source)


def _held(machine: _Machine, name: str) -> str | None:
    refusal = plate_state.start_refusal(machine, file_name=name)
    return None if refusal is None else refusal["error"]["code"]


# ---------------------------------------------------------------------------
# The recovery of the print on the plate
# ---------------------------------------------------------------------------


def test_a_recovery_of_the_plates_print_starts_with_a_yes_to_it(machine, tmp_path):
    name = _recovery(machine, tmp_path, _kiln_printed(machine, tmp_path))
    _said_yes(name)
    assert _held(machine, name) is None


@pytest.mark.parametrize("standing", [None, SOURCE_WINDOW], ids=["no-yes", "standing-window"])
def test_without_a_yes_to_this_print_it_waits_with_a_look(machine, tmp_path, standing):
    """A part can lift off the bed as it cools: a standing yes, or none, is
    not the person looking at it."""
    name = _recovery(machine, tmp_path, _kiln_printed(machine, tmp_path))
    if standing:
        _said_yes(name, source=standing)
    refusal = plate_state.start_refusal(machine, file_name=name)
    assert refusal["error"]["code"] == plate_state.RECOVERY_NEEDS_A_YES_CODE
    assert "still stuck down" in refusal["error"]["message"]


def test_the_recovery_is_still_a_new_print_to_every_gate(machine, monkeypatch, tmp_path):
    """Passing the plate hold is all a recovery gets: it is not a resume, so
    the yes and the preview are still asked for."""
    name = _recovery(machine, tmp_path, _kiln_printed(machine, tmp_path))
    assert print_signoff.verified_resume(name, {"resume_from_paused": True}, printer_name="garage") is False
    block = server._preview_gate_error("start_print", name, None, printer_name="garage", is_resume=True)
    assert block is not None and block["success"] is False


def test_the_person_is_asked_even_inside_a_standing_window_and_told_what_to_check(machine, monkeypatch, tmp_path):
    name = _recovery(machine, tmp_path, _kiln_printed(machine, tmp_path))
    asked: list[str] = []
    monkeypatch.setattr(server, "host_can_ask_the_user", lambda mcp, ctx: True)
    monkeypatch.setattr(server, "consent_for", lambda **_k: SimpleNamespace(source=SOURCE_WINDOW))

    async def _ask(ctx, message, **_kw):
        asked.append(message)
        from kiln.print_consent import DialogAnswer

        return DialogAnswer("decline", "")

    monkeypatch.setattr(server, "ask_user_to_confirm", _ask)

    async def _run():
        return await server._obtain_print_consent(
            "start_print", {"file_name": name, "printer_name": "garage"}, SimpleNamespace(),
        )

    with contextlib.suppress(RuntimeError):  # the person said no
        asyncio.run(_run())
    assert len(asked) == 1
    assert "still stuck down" in asked[0]


# ---------------------------------------------------------------------------
# Everything else onto a part is held, as before
# ---------------------------------------------------------------------------


def test_any_other_file_is_held(machine, tmp_path):
    _kiln_printed(machine, tmp_path)
    name = machine.upload_file(_file(tmp_path, "vase_resume_from_40.gcode", OTHER)).file_name
    _said_yes(name)
    assert _held(machine, name) == plate_state.START_NOT_YET_CODE


def test_a_recovery_built_from_other_gcode_is_held(machine, tmp_path):
    _kiln_printed(machine, tmp_path)
    name = _recovery(machine, tmp_path, _file(tmp_path, "theirs.gcode", OTHER))
    _said_yes(name)
    assert _held(machine, name) == plate_state.START_NOT_YET_CODE


def test_a_recovery_of_another_print_is_held(machine, tmp_path):
    name = _recovery(machine, tmp_path, _kiln_printed(machine, tmp_path))
    plate_state.mark_occupied(machine, plate_state.PlateJob(file="bracket.gcode.3mf"), source="kiln_started_print")
    _said_yes(name)
    assert _held(machine, name) == plate_state.START_NOT_YET_CODE


def test_a_recovery_onto_a_plate_with_another_part_beside_it_is_held(machine, tmp_path):
    name = _recovery(machine, tmp_path, _kiln_printed(machine, tmp_path))
    plate_state.mark_occupied(
        machine, plate_state.PlateJob(file="bracket.gcode.3mf"), source="kiln_started_print", keep_previous=True,
    )
    _said_yes(name)
    assert _held(machine, name) == plate_state.START_NOT_YET_CODE


def test_a_recovery_file_saved_over_is_held(machine, tmp_path):
    name = _recovery(machine, tmp_path, _kiln_printed(machine, tmp_path))
    other = tmp_path / "elsewhere"
    other.mkdir()
    machine.upload_file(_file(other, name, OTHER))
    _said_yes(name)
    assert _held(machine, name) == plate_state.START_NOT_YET_CODE
