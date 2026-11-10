"""The terminal's doors onto the camera add-on.

``kiln doctor``, ``kiln snapshot`` and ``kiln report`` say, in the add-on's own
words, when a printer's camera waits on the add-on, with the command that adds
it; a camera that needs nothing is not mentioned.  The command each door prints is a real command of this CLI.
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from unittest.mock import MagicMock

import click
import pytest
from click.testing import CliRunner

from kiln import camera_addon
from kiln.cli.install_camera import install_camera
from kiln.cli.main import cli
from kiln.printers.base import CameraStreamError, JobProgress, PrinterState, PrinterStatus
from tests._camera_addon_fakes import bare_computer, forget_package, write_package
from tests.test_camera_addon import BANNED

_JPEG = b"\xff\xd8\xff\xe0" + b"0" * 64 + b"\xff\xd9"
_HERMES = "hermes plugins update kiln"


@pytest.fixture
def home(monkeypatch, tmp_path):
    yield bare_computer(monkeypatch, tmp_path)
    forget_package()


class _Printer:
    """The adapter a door builds, as far as the camera doors ask it."""

    name = "bambu"

    def __init__(self, *, needs_addon: object = True, refusal: BaseException | None = None) -> None:
        self._needs_addon = needs_addon
        self._refusal = refusal

    def get_state(self) -> PrinterState:
        return PrinterState(connected=True, state=PrinterStatus.IDLE, tool_temp_actual=24.0, bed_temp_actual=23.0)

    def get_job(self) -> JobProgress:
        return JobProgress(file_name="part.gcode", completion=40.0)

    def camera_needs_addon(self) -> object:
        return self._needs_addon

    def get_snapshot(self) -> bytes:
        if self._refusal is not None:
            raise self._refusal
        return _JPEG


def _says_no_tool_name(text: str) -> None:
    said = text.lower()
    for word in BANNED:
        assert word not in said, (word, text)


# --- the command itself --------------------------------------------------------


def test_install_camera_is_a_command_of_the_kiln_cli():
    assert cli.get_command(click.Context(cli), "install-camera") is install_camera


def test_the_command_a_door_prints_runs_here(home, monkeypatch):
    printed = camera_addon.command()
    words = shlex.split(printed)
    assert words[0] == "kiln"
    monkeypatch.setattr(
        camera_addon, "install", lambda **_kw: {"ok": True, "status": "installed", "message": "done"}
    )

    result = CliRunner().invoke(cli, [*words[1:], "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["data"]["status"] == "installed"


# --- kiln doctor -----------------------------------------------------------------


def _doctor(monkeypatch, tmp_path, printer, *, name: str | None = None, json_mode: bool = True):
    monkeypatch.setenv("HOME", str(tmp_path))
    built: list[dict] = []

    def _make(cfg):
        built.append(cfg)
        return printer

    monkeypatch.setattr(
        "kiln.cli.main.load_printer_config", lambda *_a, **_k: {"type": "bambu", "host": "192.0.2.60"}
    )
    monkeypatch.setattr("kiln.cli.main._make_adapter", _make)
    # The print-door walk reads every source file; it answers its own check, not this one.
    monkeypatch.setattr("kiln.print_doors.summarize", lambda: (True, "not walked here"))
    args = (["--printer", name] if name else []) + ["doctor"] + (["--json"] if json_mode else [])
    return CliRunner().invoke(cli, args), built


def _camera_check(output: str) -> dict | None:
    return next((c for c in json.loads(output)["checks"] if c["name"] == "camera"), None)


class TestDoctor:
    def test_a_camera_waiting_on_the_addon_is_named_with_the_command(self, home, monkeypatch, tmp_path):
        result, built = _doctor(monkeypatch, tmp_path, _Printer(), name="Garage P2S")

        assert _camera_check(result.output) == {
            "name": "camera",
            "ok": True,
            "warn": True,
            "detail": camera_addon.WORDS["doctor_needed"].format(printer="Garage P2S", command="kiln install-camera"),
            "command": "kiln install-camera",
        }
        assert len(built) == 1  # the adapter the checkup already had; nothing else is built

    def test_the_line_a_person_reads(self, home, monkeypatch, tmp_path):
        result, _ = _doctor(monkeypatch, tmp_path, _Printer(), name="Garage P2S", json_mode=False)

        line = (
            "  ⚠ Camera: Garage P2S's camera needs Kiln's free camera add-on. "
            "Add it with: kiln install-camera"
        )
        assert line in result.output.splitlines()
        _says_no_tool_name(line)

    def test_the_active_printer_is_your_printer(self, home, monkeypatch, tmp_path):
        result, _ = _doctor(monkeypatch, tmp_path, _Printer())

        assert _camera_check(result.output)["detail"].startswith("Your printer's camera needs")

    @pytest.mark.parametrize("answer", [False, 1, "yes", None])
    def test_a_camera_that_needs_nothing_is_not_mentioned(self, home, monkeypatch, tmp_path, answer):
        result, _ = _doctor(monkeypatch, tmp_path, _Printer(needs_addon=answer))

        assert _camera_check(result.output) is None

    def test_an_adapter_that_cannot_say_is_not_mentioned(self, home, monkeypatch, tmp_path):
        printer = MagicMock(wraps=_Printer())
        printer.camera_needs_addon.side_effect = RuntimeError("no answer")

        result, _ = _doctor(monkeypatch, tmp_path, printer)

        assert _camera_check(result.output) is None
        assert json.loads(result.output)["status"] == "ok"

    def test_the_addon_in_place_is_one_quiet_line(self, home, monkeypatch, tmp_path):
        write_package(camera_addon.addon_dir())

        result, _ = _doctor(monkeypatch, tmp_path, _Printer())
        assert _camera_check(result.output) == {
            "name": "camera",
            "ok": True,
            "detail": camera_addon.WORDS["doctor_ready"],
            "source": "addon",
        }
        text, _ = _doctor(monkeypatch, tmp_path, _Printer(), json_mode=False)
        assert "  ✓ Camera: add-on ready" in text.output.splitlines()

    def test_a_managed_install_names_the_program_that_adds_it(self, home, monkeypatch, tmp_path):
        monkeypatch.setenv("KILN_UPDATE_COMMAND", _HERMES)

        check = _camera_check(_doctor(monkeypatch, tmp_path, _Printer(), name="Garage P2S")[0].output)

        assert check["command"] == _HERMES
        assert check["detail"] == camera_addon.WORDS["doctor_needed"].format(printer="Garage P2S", command=_HERMES)


# --- kiln snapshot -----------------------------------------------------------------


def _snapshot(monkeypatch, printer, *args):
    monkeypatch.setattr(
        "kiln.cli.main.load_printer_config", lambda *_a, **_k: {"type": "bambu", "host": "192.0.2.60"}
    )
    monkeypatch.setattr("kiln.cli.main.validate_printer_config", lambda _cfg: (True, None))
    monkeypatch.setattr("kiln.cli.main._make_adapter", lambda _cfg: printer)
    return CliRunner().invoke(cli, ["snapshot", *args])


class TestSnapshot:
    @pytest.mark.parametrize("yours, words", [(False, "needed"), (True, "needed_yours")])
    def test_the_refusal_is_the_sentence_and_the_command(self, home, monkeypatch, yours, words):
        result = _snapshot(monkeypatch, _Printer(refusal=camera_addon.refusal(yours=yours)))

        assert result.exit_code == 1
        assert result.output.splitlines() == [camera_addon.WORDS[words], "Add it with: kiln install-camera"]
        _says_no_tool_name(result.output)

    def test_json_carries_the_code_and_the_block_an_agent_reads(self, home, monkeypatch):
        result = _snapshot(monkeypatch, _Printer(refusal=camera_addon.refusal()), "--json")

        payload = json.loads(result.output)
        assert result.exit_code == 1
        assert payload["status"] == "error"
        assert payload["error"] == {"code": "FFMPEG_REQUIRED", "message": camera_addon.WORDS["needed"]}
        assert payload["data"] == {"camera_addon": camera_addon.block(moment="snapshot")}

    def test_any_other_camera_refusal_is_said_as_it_was(self, home, monkeypatch):
        refusal = CameraStreamError("The camera did not answer.", code="CAMERA_UNREACHABLE")

        payload = json.loads(_snapshot(monkeypatch, _Printer(refusal=refusal), "--json").output)
        text = _snapshot(monkeypatch, _Printer(refusal=refusal)).output

        assert payload["error"]["message"] == "The camera did not answer."
        assert "data" not in payload
        assert "Add it with" not in text


# --- kiln report -------------------------------------------------------------------


def _report(monkeypatch, printer, *args):
    monkeypatch.setattr(
        "kiln.cli.main.load_printer_config", lambda *_a, **_k: {"type": "bambu", "host": "192.0.2.60"}
    )
    monkeypatch.setattr("kiln.cli.main.validate_printer_config", lambda _cfg: (True, None))
    monkeypatch.setattr("kiln.cli.main._make_adapter", lambda _cfg: printer)
    return CliRunner().invoke(cli, ["report", *args])


class TestReport:
    def test_the_camera_line_is_the_sentence_and_the_command(self, home, monkeypatch):
        result = _report(monkeypatch, _Printer(refusal=camera_addon.refusal()))

        line = f"Camera: {camera_addon.WORDS['needed']} Add it with: kiln install-camera"
        assert line in result.output.splitlines(), result.output
        _says_no_tool_name(line)

    def test_json_carries_the_block_and_no_path(self, home, monkeypatch):
        result = _report(monkeypatch, _Printer(refusal=camera_addon.refusal()), "--json")

        data = json.loads(result.output)["data"]
        assert data["snapshot_path"] is None
        assert data["camera_addon"] == camera_addon.block(moment="snapshot")

    def test_a_failed_capture_is_never_a_path(self, home, monkeypatch):
        refusal = CameraStreamError("The camera did not answer.", code="CAMERA_UNREACHABLE")

        data = json.loads(_report(monkeypatch, _Printer(refusal=refusal), "--json").output)["data"]

        assert data["snapshot_path"] is None
        assert "camera_addon" not in data

    def test_a_picture_is_a_path(self, home, monkeypatch):
        data = json.loads(_report(monkeypatch, _Printer(), "--json").output)["data"]

        assert Path(data["snapshot_path"]).read_bytes() == _JPEG

