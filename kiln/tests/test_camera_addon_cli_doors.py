"""The terminal's doors onto the camera add-on.

``kiln doctor``, ``kiln snapshot``, ``kiln report`` and the commands that save
a printer say, in the add-on's own words, when a printer's camera waits on the
add-on, with the command that adds it; a camera that needs nothing is not
mentioned.  The command each door prints is a real command of this CLI.  Both
front pages say what the camera does, in the same words, for the models the
code names, and Kiln's own launchers bring Kiln alone: the add-on comes on a
yes.
"""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path
from unittest.mock import MagicMock, patch

import click
import pytest
from click.testing import CliRunner

from kiln import camera_addon
from kiln.cli.install_camera import install_camera
from kiln.cli.main import cli
from kiln.printers.base import CameraHandshake, CameraStreamError, JobProgress, PrinterState, PrinterStatus
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

    def test_a_managed_install_names_the_line_that_adds_it_there(self, home, monkeypatch, tmp_path):
        monkeypatch.setenv("KILN_UPDATE_COMMAND", _HERMES)
        monkeypatch.setenv("KILN_CLI", "uvx --from kiln3d==1.5.1 kiln")

        check = _camera_check(_doctor(monkeypatch, tmp_path, _Printer(), name="Garage P2S")[0].output)

        line = "uvx --from kiln3d==1.5.1 kiln install-camera"
        assert check["command"] == line
        assert check["detail"] == camera_addon.WORDS["doctor_needed"].format(printer="Garage P2S", command=line)


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


# --- the commands that save a printer -------------------------------------------------


def _cli_line() -> str:
    return camera_addon.WORDS["cli_line"].format(command="kiln install-camera")


def _joined_tail() -> str:
    """The add-on line as it reads after the handshake sentence: the label said once."""
    from kiln.camera_check import join_camera_lines

    return join_camera_lines("x", _cli_line()).removeprefix("x ")


class TestKilnAuthUsb:
    def _auth(self, tmp_path, *args):
        from kiln.cli.config import PrinterSaveResult

        def _opened(*_a, **_k):
            raise AssertionError("a USB adapter was built, which opens the port")

        with (
            patch(
                "kiln.cli.main.save_printer",
                return_value=PrinterSaveResult(tmp_path / "config.yaml", "bench", "added"),
            ),
            patch("kiln.cli.main._make_adapter", side_effect=_opened),
        ):
            return CliRunner().invoke(
                cli,
                [
                    "auth", "--name", "bench", "--host", "/dev/ttyUSB0", "--type", "usb",
                    "--printer-model", "ender3", "--camera-stream-url", "rtsp://192.0.2.7/live", *args,
                ],
            )

    def test_a_stream_camera_is_told_about_the_addon(self, home, tmp_path):
        result = self._auth(tmp_path)

        assert result.exit_code == 0, result.output
        assert _joined_tail() in result.output.replace("\n", " "), result.output

    def test_json_carries_the_line(self, home, tmp_path):
        result = self._auth(tmp_path, "--json")

        line = json.loads(result.output)["data"]["camera_check"]["line"]
        assert line.startswith("Camera: ") and line.endswith(f". {_joined_tail()}")
        assert line.count("Camera:") == 1

    def test_with_the_addon_in_place_nothing_is_said_about_it(self, home, tmp_path):
        write_package(camera_addon.addon_dir())

        line = json.loads(self._auth(tmp_path, "--json").output)["data"]["camera_check"]["line"]

        assert "add-on" not in line


def _helper_with_addon_line(line: str):
    handshake = CameraHandshake(True, "printer", "The printer's camera answered.")

    def camera_setup_line(_adapter, *, door: str):
        assert door == "cli"
        return handshake, line

    return camera_setup_line


class TestTheHelpersLineReachesTheTerminal:
    """The doors that echo ``camera_setup_line`` show the add-on line whole."""

    def _line(self) -> str:
        return f"Camera: answering (built-in). {_cli_line()}"

    def test_setup(self, home, monkeypatch, tmp_path):
        from kiln.discovery import DiscoveredPrinter

        monkeypatch.setattr("kiln.cli.config.get_config_path", lambda: tmp_path / "config.yaml")
        found = DiscoveredPrinter(host="192.168.1.9", port=7125, printer_type="moonraker", name="K1")
        with (
            patch("kiln.cli.discovery.discover_printers", return_value=[found]),
            patch("kiln.cli.printer_model_prompt.prompt_for_printer_model", return_value="k1c"),
            patch("kiln.cli.main._make_adapter", return_value=_Printer()),
            patch("kiln.camera_check.camera_setup_line", _helper_with_addon_line(self._line())),
        ):
            result = CliRunner().invoke(cli, ["setup"], input="1\nk1\n\n" + "\n" * 8)

        assert "Connected!" in result.output, result.output
        assert f"  {self._line()}" in result.output.splitlines()

    def test_quickstart(self, home, monkeypatch):
        monkeypatch.setattr(
            "kiln.cli.main._list_printers",
            lambda: [{"name": "p2s", "type": "bambu", "host": "192.0.2.60", "active": True}],
        )
        monkeypatch.setattr("kiln.cli.main.load_printer_config", lambda *_a, **_k: {"type": "bambu", "host": "x"})
        monkeypatch.setattr("kiln.cli.main._make_adapter", lambda _cfg: _Printer())
        monkeypatch.setattr("kiln.cli.discovery.discover_printers", lambda *_a, **_k: [])
        monkeypatch.setattr("kiln.camera_check.camera_setup_line", _helper_with_addon_line(self._line()))

        text = CliRunner().invoke(cli, ["quickstart"])
        data = json.loads(CliRunner().invoke(cli, ["quickstart", "--json"]).output)["data"]

        assert f"    {self._line()}" in text.output.splitlines(), text.output
        assert data["status"]["camera_check"]["line"] == self._line()

    def test_bridge_enable_first_printer(self, home, monkeypatch, capsys):
        import kiln.cli.bridge_commands as bcmd
        import kiln.cli.config as cfg
        import kiln.cli.discovery as disc
        import kiln.cli.printer_model_prompt as pmp
        from kiln.discovery import DiscoveredPrinter

        monkeypatch.setattr(cfg, "list_printers", lambda **_kw: [])
        monkeypatch.setattr(cfg, "save_printer", lambda *_a, **_kw: "/tmp/config.yaml")
        monkeypatch.setattr(cfg, "load_printer_config", lambda _name: {"type": "moonraker", "host": "x"})
        monkeypatch.setattr("kiln.cli.main._make_adapter", lambda _cfg: _Printer())
        monkeypatch.setattr(bcmd, "_onboarding_interactive", lambda: True)
        found = DiscoveredPrinter(host="192.168.1.23", port=7125, printer_type="moonraker", name="K1C")
        monkeypatch.setattr(disc, "discover_printers", lambda *_a, **_kw: [found])
        monkeypatch.setattr(pmp, "prompt_for_printer_model", lambda *_a, **_kw: "k1c")
        monkeypatch.setattr(click, "confirm", lambda *_a, **_kw: True)
        monkeypatch.setattr("kiln.camera_check.camera_setup_line", _helper_with_addon_line(self._line()))

        bcmd._offer_first_printer()

        assert f"  {self._line()}" in capsys.readouterr().out.splitlines()


# --- the front pages -------------------------------------------------------------

_ROOT = Path(__file__).resolve().parents[2]
_PAGES = (_ROOT / "README.md", _ROOT / "kiln" / "README.md")
#: Besides the add-on's own banned words, a page never names how a camera
#: sends its picture or the flag the adapter reports.  Commands are exempt.
_NOT_ON_A_PAGE = (*BANNED, "can_snapshot", "tls", "jpeg")


def _camera_paragraph(page: Path) -> str:
    lines = page.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("**Camera.**"))
    taken = []
    for line in lines[start:]:
        if not line.strip() or line.startswith("<"):
            break
        taken.append(line.strip())
    return " ".join(taken)


class TestTheFrontPages:
    def test_both_say_the_same_thing(self):
        assert _camera_paragraph(_PAGES[0]) == _camera_paragraph(_PAGES[1])

    @pytest.mark.parametrize("page", _PAGES, ids=lambda p: p.relative_to(_ROOT).as_posix())
    def test_a_page_says_what_never_how(self, page):
        said = re.sub(r"`[^`]*`", "", _camera_paragraph(page)).lower()
        for word in _NOT_ON_A_PAGE:
            assert word not in said, (page, word)

    @pytest.mark.parametrize("page", _PAGES, ids=lambda p: p.relative_to(_ROOT).as_posix())
    def test_a_page_names_the_models_the_code_names(self, page):
        from kiln.printers import bambu

        sentences = re.split(r"(?<=\.)\s+", _camera_paragraph(page))
        live = next(s for s in sentences if "live" in s).lower()
        addon = next(s for s in sentences if "need a free camera add-on" in s)
        for family in bambu._PORT6000_FAMILIES:
            assert family.replace("_", " ") in live, family
        for family in bambu._RTSPS_FAMILIES:
            series = "H" if family.startswith("h") else family[:2].upper()
            assert re.search(rf"\b{series}\b", addon), family


# --- Kiln's own launchers --------------------------------------------------------


def _launchers() -> dict[str, tuple[str, str]]:
    """Each launcher's (program that starts Kiln, the Kiln it asks for)."""
    import tomllib

    hermes = json.loads((_ROOT / "plugins" / "hermes" / "mcp.json").read_text(encoding="utf-8"))["mcpServers"]["kiln"]
    bundle = json.loads((_ROOT / "mcpb" / "manifest.json").read_text(encoding="utf-8"))["server"]["mcp_config"]
    wrapper = tomllib.loads((_ROOT / "mcpb" / "pyproject.toml").read_text(encoding="utf-8"))
    (bundle_requirement,) = wrapper["project"]["dependencies"]
    return {"hermes": (hermes["command"], hermes["args"][1]), "bundle": (bundle["command"], bundle_requirement)}


@pytest.mark.parametrize("launcher", ["hermes", "bundle"])
def test_kilns_own_launchers_bring_kiln_alone_and_start_it_through_uv(launcher):
    """The add-on comes on a yes, into Kiln's own folder, through the uv that started Kiln."""
    program, requirement = _launchers()[launcher]
    assert re.fullmatch(r"kiln3d(==\S+)?", requirement), requirement
    assert program in ("uv", "uvx")
