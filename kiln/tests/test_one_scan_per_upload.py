"""Every upload door scans a print file once, and every door scans it.

Two gaps a temperature check at one door leaves behind:

* A 3MF sent through the upload tool is scanned once, never again by the
  adapter's upload wrapper: a 100 MB plate takes about 23 s to scan, so a
  second scan doubles the wait.  The tool hands the adapter a path carrying
  its scan (``print_file_scan.scan_for_upload``), and the adapter reads it
  unless the file changed or was judged for another printer or dialect.  A
  backend that forwards to another (Creality to Moonraker) and a pipeline
  (whose safety step scans the file it then uploads) hand it on the same
  way.
* Bare G-code sent by ``kiln upload`` or a plugin calling
  ``adapter.upload_file`` is temperature-checked too: the adapter scans it.
  The lines a real file carries that the scan refuses are judged the way
  the printer reads them (``data/slicer_start_end``: a sample of real
  slicer start and end G-code).

The scan reads every heater target to the end of the file: a scan that
stops at a warning count passes a file that warns early (a fast part on a
slow hotend warns on nearly every move) with every later target unread.
"""

from __future__ import annotations

import os
import time
import zipfile
from pathlib import Path

import pytest

import kiln.printers.print_file_scan as pfs
import kiln.server as srv
from kiln.gcode import GCodeDialect, scan_gcode_file
from kiln.printers import PrinterError
from kiln.printers.base import PrinterAdapter, PrintResult, UploadResult

_DATA = Path(__file__).parent / "data" / "slicer_start_end"

_BAMBU_HEAD = "; HEADER_BLOCK_START\n; BambuStudio 02.02.02.56\n; HEADER_BLOCK_END\n"


class _Printer(PrinterAdapter):
    """A backend declared as an A1 that keeps what it was sent."""

    name = "kilntest"
    _safety_profile_id = "bambu_a1"

    def __init__(self) -> None:
        self.uploads: list[str] = []

    def upload_file(self, file_path: str) -> UploadResult:
        self.uploads.append(file_path)
        return UploadResult(success=True, file_name=os.path.basename(file_path), message="ok")

    def _start_print_impl(self, file_name: str, **kwargs) -> PrintResult:
        return PrintResult(success=True, message="started")


class _Forwarding(PrinterAdapter):
    """A backend that hands its uploads to another adapter, as Creality's
    hands them to Moonraker."""

    name = "kilntest"
    _safety_profile_id = "bambu_a1"

    def __init__(self) -> None:
        self.backend = _Printer()

    def upload_file(self, file_path: str) -> UploadResult:
        return self.backend.upload_file(file_path)

    def _start_print_impl(self, file_name: str, **kwargs) -> PrintResult:
        return PrintResult(success=True, message="started")


_Printer.__abstractmethods__ = frozenset()
_Forwarding.__abstractmethods__ = frozenset()


def _plate(tmp_path: Path, nozzle: int, name: str = "part.gcode.3mf") -> Path:
    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("Metadata/plate_1.gcode", f"{_BAMBU_HEAD}G28\nM104 S{nozzle}\nG1 X10 Y10 F3000\n")
    return path


def _gcode(tmp_path: Path, nozzle: int, name: str = "part.gcode") -> Path:
    path = tmp_path / name
    path.write_text(f"G28\nM104 S{nozzle}\nG1 X10 Y10 F3000\n")
    return path


@pytest.fixture
def scans(monkeypatch) -> list[str]:
    """Every file the shared reader scans, in order."""
    seen: list[str] = []
    real = pfs.scan_print_file

    def counted(path, **kwargs):
        seen.append(os.path.basename(str(path)))
        return real(path, **kwargs)

    monkeypatch.setattr(pfs, "scan_print_file", counted)
    return seen


@pytest.fixture
def aimed(monkeypatch) -> _Printer:
    """The upload tool aimed at a ``_Printer`` declared as an A1."""
    printer = _Printer()
    monkeypatch.setattr(srv, "_resolve_control_target", lambda name: (printer, name or "default"))
    monkeypatch.setattr(srv, "_resolve_printer_model_live", lambda name=None: "bambu_a1")
    monkeypatch.setattr(srv, "_PRINTER_MODEL", "bambu_a1")
    monkeypatch.setattr(srv, "_CONFIRM_UPLOAD", False)
    monkeypatch.setattr(srv, "_check_rate_limit", lambda tool: None)
    return printer


def test_the_upload_tool_scans_what_it_sends_once(aimed, scans, tmp_path):
    plate = _plate(tmp_path, 220)
    gcode = _gcode(tmp_path, 220)

    assert srv.upload_file(str(plate)).get("success") is not False
    assert scans == [plate.name], "the adapter scanned the plate the tool had just scanned"
    scans.clear()
    assert srv.upload_file(str(gcode)).get("success") is not False
    assert scans == [gcode.name]
    assert [os.path.basename(u) for u in aimed.uploads] == [plate.name, gcode.name]


def test_kiln_upload_refuses_bare_gcode_that_heats_past_the_printer(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from kiln.cli.main import cli

    printer = _Printer()
    hot = _gcode(tmp_path, 400, "hot.gcode")
    monkeypatch.setattr("kiln.cli.main._get_adapter_from_ctx", lambda ctx: printer)

    result = CliRunner().invoke(cli, ["upload", str(hot), "--json"])

    assert result.exit_code == 1, result.output
    assert "GCODE_BLOCKED" in result.output and "M104 S400 exceeds" in result.output
    assert printer.uploads == []
    # A plugin calling the adapter directly meets the same refusal.
    with pytest.raises(PrinterError, match="GCODE_BLOCKED"):
        printer.upload_file(str(hot))
    assert printer.uploads == []


def test_a_backend_that_forwards_an_upload_scans_it_once(scans, tmp_path):
    printer = _Forwarding()
    plate = _plate(tmp_path, 220)

    printer.upload_file(str(plate))

    assert scans == [plate.name]
    assert [os.path.basename(u) for u in printer.backend.uploads] == [plate.name]


def test_a_file_changed_after_its_scan_is_scanned_again(scans, tmp_path):
    printer = _Printer()
    path = _gcode(tmp_path, 220)
    carried, scan = pfs.scan_for_upload(str(path), printer_id="bambu_a1", dialect=GCodeDialect.GENERIC)
    assert scan.valid
    time.sleep(0.01)
    path.write_text("G28\nM104 S400\n")  # rewritten after the door judged it

    with pytest.raises(PrinterError, match="M104 S400 exceeds"):
        printer.upload_file(carried)
    assert scans == [path.name, path.name]


def test_a_scan_for_another_printer_or_dialect_is_not_borrowed(scans, tmp_path):
    printer = _Printer()  # an A1, scanned in the Bambu dialect for a 3MF
    plate = _plate(tmp_path, 220)
    for pid, dialect in (("ender3", GCodeDialect.BAMBU), ("bambu_a1", GCodeDialect.KLIPPER)):
        scans.clear()
        carried, _ = pfs.scan_for_upload(str(plate), printer_id=pid, dialect=dialect)
        printer.upload_file(carried)
        assert scans == [plate.name, plate.name], (pid, dialect)
    for spelling in ("Bambu-A1", "Bambu Lab A1"):
        scans.clear()
        carried, _ = pfs.scan_for_upload(str(plate), printer_id=spelling, dialect=GCodeDialect.BAMBU)
        printer.upload_file(carried)
        assert scans == [plate.name], f"{spelling!r} is the printer the adapter calls bambu_a1"


def test_the_scan_reads_the_whole_file_past_its_warning_cap(tmp_path):
    path = tmp_path / "warns.gcode"
    # Each move below the bed plane warns; the hot line comes after the cap.
    path.write_text("G28\n" + "G1 Z-0.1\n" * 60 + "M104 S400\n")

    scan = scan_gcode_file(str(path), macros=True, keep_commands=False)

    assert not scan.valid
    assert scan.errors == ["M104 S400 exceeds maximum hotend temperature (300C)"]


def test_no_real_slicer_file_is_refused(tmp_path):
    """Real start and end G-code from vendor presets, read as files: a
    Klipper start macro passes with only its heat judged, and a printer
    whose own limits Kiln cannot read is judged on the general limits."""
    files = sorted(_DATA.glob("*.gcode"))
    assert len(files) >= 10
    refused = []
    for path in files:
        for dialect in (GCodeDialect.GENERIC, GCodeDialect.KLIPPER):
            for printer_id in (None, "a_printer_kiln_has_no_record_of"):
                scan = pfs.scan_print_file(str(path), printer_id=printer_id, dialect=dialect)
                if not scan.valid:
                    refused.append((path.name, dialect.value, printer_id, scan.errors[:1]))
    assert refused == []


@pytest.mark.parametrize("line", ["#M104 S400", "0 M104 S400", "@M104 S400"])
def test_a_line_that_does_not_start_with_a_letter_is_judged_on_what_klipper_runs(tmp_path, line):
    """Klipper reads a line from its first letter, so these heat the hotend."""
    path = tmp_path / "f.gcode"
    path.write_text(f"G28\n{line}\n")

    scan = pfs.scan_print_file(str(path), printer_id=None, dialect=GCodeDialect.KLIPPER)

    assert not scan.valid
    assert scan.errors == ["M104 S400 exceeds maximum hotend temperature (300C)"]


def _pipeline_adapter(tmp_path: Path, wraps: bool) -> PrinterAdapter:
    """A printer for a pipeline that is offline at preflight, so the run
    stops after the upload; a Bambu-like one wraps the G-code as a 3MF."""
    from kiln.printers.base import PrinterState, PrinterStatus

    class _Target(_Printer):
        def get_state(self) -> PrinterState:
            return PrinterState(state=PrinterStatus.OFFLINE, connected=False)

    if wraps:
        def wrap_gcode_as_3mf(self, gcode_path: str, **kwargs) -> str:
            out = tmp_path / "wrapped.gcode.3mf"
            with zipfile.ZipFile(out, "w") as zf:
                zf.writestr("Metadata/plate_1.gcode", _BAMBU_HEAD + Path(gcode_path).read_text())
            return str(out)

        _Target.wrap_gcode_as_3mf = wrap_gcode_as_3mf
    _Target.__abstractmethods__ = frozenset()
    return _Target()


@pytest.mark.parametrize("wraps", [True, False], ids=["wrapped_3mf", "raw_gcode"])
def test_a_pipeline_scans_the_file_it_uploads_once(tmp_path, monkeypatch, wraps):
    from unittest.mock import MagicMock, patch

    import kiln.gcode as gcode_mod
    from kiln.pipelines import reslice_and_print

    sliced = _gcode(tmp_path, 220, "out.gcode")
    adapter = _pipeline_adapter(tmp_path, wraps)
    read: list[str] = []
    real = gcode_mod.scan_gcode_file

    def counted(path, **kwargs):
        read.append(os.path.basename(str(path)))
        return real(path, **kwargs)

    # The underlying reader, under both names it is reached by.
    monkeypatch.setattr(gcode_mod, "scan_gcode_file", counted)
    monkeypatch.setattr(pfs, "scan_gcode_file", counted)
    slice_result = MagicMock(output_path=str(sliced), message="Sliced OK", slicer="prusaslicer")
    with (
        patch("kiln.slicer.slice_file", return_value=slice_result),
        patch("kiln.slicer_profiles.resolve_slicer_profile", return_value=str(tmp_path / "p.ini")),
        patch("kiln.server._resolve_adapter", return_value=adapter),
    ):
        result = reslice_and_print(
            model_path=str(tmp_path / "model.stl"), printer_name="p", printer_id="bambu_a1", skip_validation=True,
        )

    upload = [s for s in result.steps if s.name == "upload"]
    assert upload and upload[0].success, [s.message for s in result.steps]
    assert len(adapter.uploads) == 1
    assert len(read) == 1, f"scanned {read} for one upload"
