"""One heater, one limit; and a print nobody said go to leaves nothing behind.

* On a printer whose model Kiln has not been told, every door that sends
  commands (``send_gcode``, both validate tools, ``kiln gcode``) holds the
  hotend and bed to the floor ``set_temperature`` holds them to, through
  :func:`kiln.gcode.validate_commands`.  A printer whose model is known is
  judged by its own profile.  A print FILE a slicer made keeps its own rule
  (the generic limits when the printer's own are not in hand), pinned below
  so it is not swept up.

* ``slice_and_print``, and ``retry_print_with_fix`` when the retry changes
  the object, ask whether anybody said go before the sliced file is sent,
  so a refused print leaves no file on the printer.  Slicing is unchanged,
  and the full gate still judges the start.
"""
from __future__ import annotations

import os
import struct
from unittest.mock import MagicMock, patch

import pytest

from kiln import consent_windows, preview_evidence, print_consent, print_signoff, server
from kiln.gcode import scan_gcode_file
from kiln.print_consent import SOURCE_ELICITED, PrintConsent, register_hosted_approval_hook, reset_consent, set_consent
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
from kiln.printers.octoprint import OctoPrintAdapter
from kiln.registry import PrinterRegistry


@pytest.fixture(autouse=True)
def _unidentified(monkeypatch, tmp_path):
    """A fresh home, no consent on record, and a printer whose model nobody
    has told Kiln."""
    monkeypatch.setenv("KILN_HOME", str(tmp_path / "home"))
    for name in ("KILN_SKIP_PREVIEW_GATE", "KILN_HOSTED_MULTITENANT", "KILN_PRINTER_MODEL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("KILN_EMERGENCY_PERSIST", "0")
    preview_evidence._reset_for_tests()
    print_signoff._reset_for_tests()
    print_consent._reset_for_tests()
    consent_windows._reset_for_tests()
    monkeypatch.setattr(consent_windows, "person_at_terminal", lambda: False)
    monkeypatch.setattr("kiln.local_stage.host_renders_apps", lambda *a, **k: False)
    register_hosted_approval_hook(None)
    monkeypatch.setattr(server, "_PRINTER_MODEL", "")
    monkeypatch.setattr(server, "_resolve_printer_model_live", lambda *a, **k: "")
    # Several calls in a row would otherwise meet the per-tool rate limit.
    monkeypatch.setattr(server._tool_limiter, "check", lambda *a, **kw: None)


def _commands(commands: list[str]):
    """The shared door check (imported here so the door tests below still
    run, and fail on their own terms, on a tree that has not got it)."""
    from kiln.gcode import validate_commands

    return validate_commands(commands)


def _floor() -> tuple[float, float]:
    """The limits ``set_temperature`` holds this printer to."""
    return server._get_temp_limits(None)


# ---------------------------------------------------------------------------
# One heater, one limit
# ---------------------------------------------------------------------------


def test_the_floor_is_the_unidentified_printer_floor():
    assert _floor() == (240.0, 80.0)


@pytest.mark.parametrize("delta", [-1, 0, 1, 60])
def test_a_raw_hotend_command_is_judged_by_the_temperature_tools_limit(delta):
    """Each side of the hotend floor, and a setting well past it."""
    hotend, _ = _floor()
    temp = hotend + delta
    for cmd in ("M104", "M109"):
        result = _commands([f"{cmd} S{temp:g}"])
        assert result.valid is (temp <= hotend), (cmd, temp, result.errors)


@pytest.mark.parametrize("delta", [-1, 0, 1, 40])
def test_a_raw_bed_command_is_judged_by_the_temperature_tools_limit(delta):
    """Each side of the bed floor, and a setting well past it."""
    _, bed = _floor()
    temp = bed + delta
    for cmd in ("M140", "M190"):
        result = _commands([f"{cmd} S{temp:g}"])
        assert result.valid is (temp <= bed), (cmd, temp, result.errors)


def test_the_refusal_says_why_the_limit_is_low_and_how_to_raise_it():
    result = _commands(["M104 S300"])
    assert not result.valid
    text = " ".join(result.errors)
    assert "240" in text and "printer_model" in text


@patch("kiln.server._get_adapter")
def test_send_gcode_refuses_a_hotend_past_the_floor_and_sends_nothing(mock_get_adapter):
    """The refusal comes before anything reaches the printer."""
    adapter = MagicMock(spec=OctoPrintAdapter)
    mock_get_adapter.return_value = adapter
    result = server.send_gcode("M104 S300")
    assert result["success"] is False, result
    adapter.send_gcode.assert_not_called()


@patch("kiln.server._get_adapter")
def test_send_gcode_refuses_a_bed_past_the_floor_and_sends_nothing(mock_get_adapter):
    """The refusal comes before anything reaches the printer."""
    adapter = MagicMock(spec=OctoPrintAdapter)
    mock_get_adapter.return_value = adapter
    result = server.send_gcode("M140 S120")
    assert result["success"] is False, result
    adapter.send_gcode.assert_not_called()


@patch("kiln.server._get_adapter")
def test_send_gcode_still_sends_a_temperature_at_the_floor(mock_get_adapter):
    adapter = MagicMock(spec=OctoPrintAdapter)
    mock_get_adapter.return_value = adapter
    result = server.send_gcode("M104 S240")
    assert result["success"] is True, result
    adapter.send_gcode.assert_called_once()


@patch("kiln.server._get_adapter")
def test_a_known_model_is_still_judged_by_its_own_limits(mock_get_adapter, monkeypatch):
    """The floor is for a printer Kiln cannot identify, nothing else."""
    seen: list[tuple] = []
    real = server.validate_commands

    def spy(commands, printer_id=None, *a, **k):
        seen.append((tuple(commands), printer_id))
        return real(commands, printer_id, *a, **k)

    monkeypatch.setattr(server, "validate_commands", spy)
    monkeypatch.setattr(server, "_resolve_printer_model_live", lambda *a, **k: "ender3")
    mock_get_adapter.return_value = MagicMock(spec=OctoPrintAdapter)
    server.send_gcode("M104 S200")
    assert seen == [(("M104 S200",), "ender3")]


def _tool(name: str):
    return server.mcp._tool_manager._tools[name].fn


def test_the_validate_gcode_preview_agrees_with_send_gcode():
    """The preview gives the answer the send would."""
    out = _tool("validate_gcode")("M104 S300")
    assert out["valid"] is False, out


def test_validate_gcode_safe_without_a_printer_judges_against_the_floor():
    """With no printer named, the answer it labels the default profile is
    judged by that profile's own limits."""
    out = _tool("validate_gcode_safe")("M104 S300")
    assert out["valid"] is False, out
    assert out["profile"]["id"] == "default"


def test_a_print_file_keeps_the_generic_limits_when_the_printers_own_are_not_in_hand(tmp_path):
    """Not swept up: a file a slicer made for a machine is not judged against
    a machine it was not made for (``scan_gcode_file``)."""
    path = tmp_path / "petg.gcode"
    path.write_text("M104 S250\nM140 S90\nG28\n")
    result = scan_gcode_file(str(path), generic_when_unread=True)
    assert result.valid is True, result.errors
    assert result.generic_limits is True


# ---------------------------------------------------------------------------
# A print nobody said go to leaves nothing on the printer
# ---------------------------------------------------------------------------


class _Printer(PrinterAdapter):
    """Records what it is told; idle and connected."""

    def __init__(self) -> None:
        self.uploads: list[str] = []
        self.starts: list[str] = []
        self._kiln_registered_name = "garage"

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
        return []

    def upload_file(self, file_path: str) -> UploadResult:
        self.uploads.append(os.path.basename(file_path))
        return UploadResult(success=True, file_name=os.path.basename(file_path), message="ok")

    def _start_print_impl(self, file_name: str, **kwargs) -> PrintResult:
        self.starts.append(file_name)
        return PrintResult(success=True, message="started")

    def cancel_print(self) -> PrintResult:
        return PrintResult(success=True, message="")

    def pause_print(self) -> PrintResult:
        return PrintResult(success=True, message="")

    def _resume_print_impl(self) -> PrintResult:
        return PrintResult(success=True, message="")

    def emergency_stop(self) -> PrintResult:
        return PrintResult(success=True, message="")

    def _load_filament_impl(self, plan):
        raise NotImplementedError

    def _unload_filament_impl(self, plan):
        raise NotImplementedError

    def _purge_filament_impl(self, plan):
        raise NotImplementedError

    def set_tool_temp(self, target: float) -> bool:
        return True

    def set_bed_temp(self, target: float) -> bool:
        return True

    def send_gcode(self, commands: list[str]) -> bool:
        return True

    def delete_file(self, file_path: str) -> bool:
        return True


def _cube(path) -> str:
    s = 20.0
    v = [(0, 0, 0), (s, 0, 0), (s, s, 0), (0, s, 0), (0, 0, s), (s, 0, s), (s, s, s), (0, s, s)]
    faces = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
             (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    with open(path, "wb") as fh:
        fh.write(b"\x00" * 80 + struct.pack("<I", len(faces)))
        for a, b, c in faces:
            fh.write(struct.pack("<fff", 0, 0, 0))
            for i in (a, b, c):
                fh.write(struct.pack("<fff", *v[i]))
            fh.write(b"\x00\x00")
    return str(path)


@pytest.fixture
def garage(monkeypatch):
    printer = _Printer()
    registry = PrinterRegistry()
    registry.register("garage", printer)
    monkeypatch.setattr(server, "_registry", registry)
    return printer


@pytest.fixture
def sliced(monkeypatch, tmp_path):
    """The slice step, standing in for the slicer (none is needed): a small
    G-code file, so a door goes on to the point where it would send the file
    to the printer."""
    gcode = tmp_path / "cube.gcode"
    gcode.write_text("G28\nM104 S200\nG1 X10 Y10 Z0.2 F1200\n")

    class _Result:
        output_path = str(gcode)

        def to_dict(self) -> dict:
            return {"success": True, "output_path": str(gcode)}

    def _placed_slice(model_path, *a, **k):
        return _Result(), None, {"effective_input": str(model_path), "placement": None, "quiet_start": None, "lift_floor_mm": None}

    monkeypatch.setattr("kiln.plugins.slicer_tools._placed_slice", _placed_slice)
    return gcode


def test_slice_and_print_with_no_yes_sends_nothing_to_the_printer(tmp_path, garage, sliced):
    """No yes on record: the sliced file is never sent, so nothing waits on
    the printer."""
    out = _tool("slice_and_print")(_cube(tmp_path / "cube.stl"), printer_name="garage", skip_validation=True)
    assert out["success"] is False
    assert out["error"]["code"] == "PREVIEW_NOT_CONFIRMED", out
    assert garage.uploads == [] and garage.starts == []


def test_slice_and_print_with_a_yes_goes_on_to_send_the_file(tmp_path, garage, sliced):
    """The question before the upload lets an approved print through; the
    full gate still judges the start (here it wants the preview too)."""
    model = _cube(tmp_path / "cube.stl")
    token = set_consent(PrintConsent(tool="slice_and_print", file_name=model, printer_name="garage", source=SOURCE_ELICITED))
    try:
        _tool("slice_and_print")(model, printer_name="garage", skip_validation=True)
    finally:
        reset_consent(token)
    assert garage.uploads == ["cube.gcode"]


def test_a_retry_that_changes_the_object_with_no_yes_sends_nothing(tmp_path, garage, sliced):
    """A retry that changes the object needs the same yes before its file
    is sent."""
    out = _tool("retry_print_with_fix")(
        _cube(tmp_path / "cube.stl"), printer_name="garage", skip_diagnosis=True, skip_validation=True,
        custom_overrides={"support_material": "1"},
    )
    assert out["success"] is False
    assert out["error"]["code"] == "PREVIEW_NOT_CONFIRMED", out
    assert garage.uploads == [] and garage.starts == []
