"""A yes starts the bytes it was given for, and no others under the same name.

A print started by its name on the printer prints whatever the printer holds
under that name.  The yes (the dialog, the code on the screen, the account's
card) and the preview are found by that name, and each records the sha256 of
what it was about; right before the print starts, the bytes that would print
must be those bytes, or a file Kiln made from them.  These tests start real
``PrinterAdapter.start_print`` templates on a printer that holds files of its
own.
"""

from __future__ import annotations

import hashlib
import os
import pathlib

import pytest

from kiln import approved_bytes, monitor_twin, print_signoff, resume_provenance
from kiln.preview_gate import PreviewGate, hash_file
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

_APPROVED = b"G28\nG1 X10 Y10 F3000\nG1 X20 Y10 E1.0\n"
_OTHER = b"G28\nG1 X10 Y10 F3000\nG1 X90 Y90 E9.0\n"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("KILN_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("KILN_SKIP_PREVIEW_GATE", raising=False)
    monkeypatch.setattr(monitor_twin, "_TWIN_DIR", tmp_path / "twin")
    monkeypatch.setattr(monitor_twin, "_SLICES_FILE", tmp_path / "twin" / "slices.json")
    print_signoff._reset_for_tests()
    # Whether a file is complete enough to print is judged by its own tests;
    # here every file is, so a refusal can only be about its bytes.
    from kiln.printers import gcode_complete

    monkeypatch.setattr(gcode_complete, "gcode_problems", lambda *_a, **_k: [])
    from kiln.printers import print_gate

    monkeypatch.setattr(print_gate, "_unknown_bed_verdict", lambda *_a, **_k: None)
    import kiln.preview_gate as pg

    monkeypatch.setattr(pg, "_gate", PreviewGate())
    with resume_provenance._lock:
        resume_provenance._sent.clear()
    yield
    print_signoff._reset_for_tests()
    with resume_provenance._lock:
        resume_provenance._sent.clear()


class _Printer(PrinterAdapter):
    """A printer that keeps the files sent to it and hands them back."""

    reads_back = True

    def __init__(self, held: dict[str, bytes] | None = None, *, size: int | None = None) -> None:
        self.held = dict(held or {})
        self.started: list[str] = []
        self.reported_size = size
        self._kiln_registered_name = "garage"
        print_signoff.require_signoff(self)

    @property
    def name(self) -> str:
        return "fake"

    @property
    def capabilities(self) -> PrinterCapabilities:
        return PrinterCapabilities()

    def get_state(self) -> PrinterState:
        return PrinterState(state=PrinterStatus.IDLE, connected=True)

    def get_job(self) -> JobProgress:
        return JobProgress()

    def list_files(self) -> list[PrinterFile]:
        return [PrinterFile(name=n, path=n, size_bytes=len(b)) for n, b in self.held.items()]

    def upload_file(self, file_path: str) -> UploadResult:
        name = os.path.basename(file_path)
        self.held[name] = pathlib.Path(file_path).read_bytes()
        return UploadResult(success=True, file_name=name, message="ok")

    def read_print_file(self, file_name: str) -> bytes | None:
        if not self.reads_back:
            return None
        return self.held[os.path.basename(file_name)]

    def print_file_identity(self, file_name: str) -> tuple[int | None, int | None] | None:
        return (self.reported_size, None) if self.reported_size is not None else None

    def _start_print_impl(self, file_name: str, **kwargs) -> PrintResult:
        self.started.append(file_name)
        return PrintResult(success=True, message="started")

    def cancel_print(self) -> PrintResult:
        return PrintResult(success=True, message="")

    def pause_print(self) -> PrintResult:
        return PrintResult(success=True, message="")

    def _resume_print_impl(self) -> PrintResult:
        return PrintResult(success=True, message="")

    def emergency_stop(self) -> PrintResult:
        return PrintResult(success=True, message="")

    def delete_file(self, file_path: str) -> bool:
        return self.held.pop(os.path.basename(file_path), None) is not None

    def set_tool_temp(self, target: float) -> bool:
        return True

    def set_bed_temp(self, target: float) -> bool:
        return True

    def send_gcode(self, commands: list[str]) -> bool:
        return True

    def _load_filament_impl(self, plan):
        raise NotImplementedError

    def _unload_filament_impl(self, plan):
        raise NotImplementedError

    def _purge_filament_impl(self, plan):
        raise NotImplementedError


class _NoReadBack(_Printer):
    """A printer with no way to hand a file back (the base default)."""

    read_print_file = PrinterAdapter.read_print_file


def _grant(name: str = "part.gcode", **shas: str) -> None:
    print_signoff.grant("start_print", name, "garage", source="user_elicited", door="stage", **shas)


class TestByNameStarts:
    def test_a_yes_for_one_file_does_not_start_another_of_the_same_name(self):
        printer = _Printer({"part.gcode": _OTHER})
        _grant(yes_sha256=_sha(_APPROVED))
        result = printer.start_print("part.gcode")
        assert result.success is False
        assert getattr(result, "code", "") == approved_bytes.CODE_NOT_APPROVED
        assert "is not the file that was approved" in result.message
        assert printer.started == []

    def test_the_approved_bytes_start(self):
        printer = _Printer({"part.gcode": _APPROVED})
        _grant(yes_sha256=_sha(_APPROVED))
        assert printer.start_print("part.gcode").success is True
        assert printer.started == ["part.gcode"]

    def test_the_preview_is_held_to_its_bytes_too(self):
        # The token keeps a shortened digest; it still names the bytes.
        printer = _Printer({"part.gcode": _OTHER})
        _grant(preview_sha256=_sha(_APPROVED)[:32])
        assert printer.start_print("part.gcode").success is False
        printer = _Printer({"part.gcode": _APPROVED})
        _grant(preview_sha256=_sha(_APPROVED)[:32])
        assert printer.start_print("part.gcode").success is True

    def test_a_yes_and_a_preview_for_different_bytes_start_nothing(self):
        printer = _Printer({"part.gcode": _APPROVED})
        _grant(yes_sha256=_sha(_APPROVED), preview_sha256=_sha(_OTHER))
        assert printer.start_print("part.gcode").success is False
        assert printer.started == []

    def test_bytes_kiln_cannot_see_do_not_start_on_a_yes_for_bytes(self):
        printer = _NoReadBack({"part.gcode": _APPROVED})
        _grant(yes_sha256=_sha(_APPROVED))
        result = printer.start_print("part.gcode")
        assert result.success is False
        assert getattr(result, "code", "") == approved_bytes.CODE_UNCHECKED
        assert "from the file on this computer" in result.message
        assert printer.started == []

    def test_bytes_kiln_sent_under_the_name_need_no_read_back(self, tmp_path):
        local = tmp_path / "part.gcode"
        local.write_bytes(_APPROVED)
        printer = _NoReadBack()
        assert printer.upload_file(str(local)).success is True
        _grant(yes_sha256=_sha(_APPROVED))
        assert printer.start_print("part.gcode").success is True

    def test_a_size_the_printer_reports_against_kilns_record_is_not_taken_on_trust(self, tmp_path):
        # Kiln sent the approved file; the printer now reports another size
        # for that name, so something replaced it and Kiln cannot read it.
        local = tmp_path / "part.gcode"
        local.write_bytes(_APPROVED)
        printer = _NoReadBack(size=len(_APPROVED) + 7)
        printer.upload_file(str(local))
        _grant(yes_sha256=_sha(_APPROVED))
        result = printer.start_print("part.gcode", local_file_path=str(local))
        assert result.success is False and printer.started == []

    def test_a_clearance_that_names_no_bytes_starts_as_before(self):
        printer = _Printer({"part.gcode": _OTHER})
        _grant()
        assert printer.start_print("part.gcode").success is True


class TestFilesKilnMadeFromTheApprovedOne:
    def test_a_slice_of_the_approved_model_starts(self, tmp_path):
        model = tmp_path / "jar.stl"
        model.write_bytes(b"solid jar\nendsolid jar\n")
        gcode = tmp_path / "jar.gcode"
        gcode.write_bytes(_APPROVED)
        monitor_twin.note_sliced(str(model), str(gcode))
        printer = _Printer({"jar.gcode": _APPROVED})
        print_signoff.grant(
            "slice_and_print", str(model), "garage", source="user_elicited", door="stage",
            preview_sha256=hash_file(str(model)),
        )
        assert printer.start_print("jar.gcode").success is True

    def test_another_file_under_the_slices_name_does_not_start(self, tmp_path):
        model = tmp_path / "jar.stl"
        model.write_bytes(b"solid jar\nendsolid jar\n")
        gcode = tmp_path / "jar.gcode"
        gcode.write_bytes(_APPROVED)
        monitor_twin.note_sliced(str(model), str(gcode))
        printer = _Printer({"jar.gcode": _OTHER})
        print_signoff.grant(
            "slice_and_print", str(model), "garage", source="user_elicited", door="stage",
            preview_sha256=hash_file(str(model)),
        )
        result = printer.start_print("jar.gcode")
        assert result.success is False and printer.started == []

    def test_a_slice_of_another_model_under_the_same_name_does_not_start(self, tmp_path):
        model = tmp_path / "jar.stl"
        model.write_bytes(b"solid jar\nendsolid jar\n")
        gcode = tmp_path / "jar.gcode"
        gcode.write_bytes(_APPROVED)
        monitor_twin.note_sliced(str(model), str(gcode))
        approved_model_bytes = b"solid a different jar\nendsolid\n"
        printer = _Printer({"jar.gcode": _APPROVED})
        print_signoff.grant(
            "slice_and_print", str(model), "garage", source="user_elicited", door="stage",
            preview_sha256=_sha(approved_model_bytes),
        )
        assert printer.start_print("jar.gcode").success is False


class TestTheBytesRideEveryRecord:
    def test_a_stored_clearance_keeps_its_bytes(self):
        _grant(yes_sha256=_sha(_APPROVED), preview_sha256=_sha(_APPROVED)[:32])
        record = print_signoff.record_for(print_signoff.current())
        print_signoff.clear()
        again = print_signoff.grant_from_record(record, tool="start_print", file_name="part.gcode", printer_name="garage")
        assert again is not None
        assert again.yes_sha256 == _sha(_APPROVED) and again.preview_sha256 == _sha(_APPROVED)[:32]
        printer = _Printer({"part.gcode": _OTHER})
        assert printer.start_print("part.gcode").success is False

    def test_the_gate_carries_the_yes_and_the_preview_onto_the_clearance(self, tmp_path, monkeypatch):
        from kiln import preview_evidence, server
        from kiln.print_consent import PrintConsent, reset_consent, set_consent

        preview_evidence._reset_for_tests()
        part = tmp_path / "part.gcode"
        part.write_bytes(_APPROVED)
        preview_evidence.record("stage", str(part), via="panel_fetch")
        monkeypatch.setattr(server, "_check_auth", lambda *_a, **_k: None)
        token = server.issue_preview_token(str(part), door="stage")["token"]
        reset = set_consent(
            PrintConsent(tool="start_print", file_name=str(part), printer_name="garage", file_sha256=_sha(_APPROVED))
        )
        try:
            assert server._preview_gate_error("start_print", str(part), token, printer_name="garage") is None
        finally:
            reset_consent(reset)
            preview_evidence._reset_for_tests()
        clearance = print_signoff.current()
        assert clearance is not None
        assert clearance.yes_sha256 == _sha(_APPROVED)
        assert approved_bytes.same(clearance.preview_sha256, _sha(_APPROVED))
