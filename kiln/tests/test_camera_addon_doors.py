"""The agent's doors to Kiln's camera add-on.

A camera that needs the add-on is refused with the add-on's own sentence,
and every door an agent reads carries the same ``camera_addon`` block beside
its answer: what to say, what to ask, and the one call that adds it after
the person says yes.  The block never rides inside ``error``.
"""

from __future__ import annotations

from typing import Any
from unittest import mock

import pytest

from kiln import camera_addon
from kiln.printers.base import CameraStreamError, StreamCapability
from tests._camera_addon_fakes import bare_computer, forget_package

BANNED = ("ffmpeg", "rtsp", "port", "pip", "imageio", "322", "6000")
INSTALL = {"tool": "upgrade_kiln", "arguments": {"addon": "camera", "confirm": True}}
MANAGED = "hermes plugins update kiln"


def _plain(text: str) -> str:
    """*text*, after checking it names no program, protocol or installer."""
    said = [word for word in BANNED if word in text.lower()]
    assert not said, f"{said} in {text!r}"
    return text


class _Printer:
    """A printer whose camera may need the add-on, and whose snapshot does what it is told."""

    camera_check_ids: tuple[str, ...] = ()
    external_camera = None

    def __init__(self, *, needs: bool = True, refusal: BaseException | None = None) -> None:
        self._needs = needs
        self._refusal = refusal
        self.snapshot_source = "printer"

    def camera_needs_addon(self) -> bool:
        return self._needs

    def get_snapshot(self) -> bytes:
        if self._refusal is not None:
            raise self._refusal
        return b"\xff\xd8\xff\xd9" * 40

    def stream_capability(self) -> StreamCapability:
        if self._needs:
            return StreamCapability(False, "rtsp", camera_addon.WORDS["needed"])
        return StreamCapability(False, "rtsp", camera_addon.WORDS["pictures_only"])


@pytest.fixture
def bare(monkeypatch, tmp_path):
    yield bare_computer(monkeypatch, tmp_path)
    forget_package()


def _addon_block(reply: dict[str, Any]) -> dict[str, Any]:
    block = reply[camera_addon.RESULT_KEY]
    assert camera_addon.RESULT_KEY not in reply.get("error", {})
    assert "agent_note" not in reply.get("error", {})
    _plain(block["message"])
    return block


# ---------------------------------------------------------------------------
# printer_snapshot
# ---------------------------------------------------------------------------


class TestTheSnapshotRefusal:
    """``printer_snapshot`` refused for want of the add-on, and refused for any other reason."""

    def _snapshot(self, printer: _Printer) -> dict[str, Any]:
        from kiln import server

        with mock.patch.object(server, "_get_adapter", return_value=printer):
            return server.printer_snapshot()

    def test_the_refusal_carries_the_block_beside_the_error(self, bare):
        reply = self._snapshot(_Printer(refusal=camera_addon.refusal()))
        assert reply["success"] is False
        assert reply["error"]["code"] == "FFMPEG_REQUIRED"
        assert _plain(reply["error"]["message"]) == camera_addon.WORDS["needed"]
        assert reply["error"]["retryable"] is False
        block = _addon_block(reply)
        assert block["status"] == "needed"
        assert block["message"] == camera_addon.WORDS["needed"]
        assert _plain(block["ask"]) == camera_addon.WORDS["ask"]
        assert block["install"] == INSTALL
        assert block["free"] is True and block["restart_required"] is False
        assert block["command"] == camera_addon.command()

    def test_a_managed_install_is_told_where_the_addon_comes_from(self, bare, monkeypatch):
        monkeypatch.setenv("KILN_UPDATE_COMMAND", MANAGED)
        block = _addon_block(self._snapshot(_Printer(refusal=camera_addon.refusal())))
        assert block["status"] == "managed"
        assert "install" not in block
        assert block["command"] == MANAGED

    @pytest.mark.parametrize("code", ["CAMERA_LIVEVIEW_OFF", "CAMERA_UNREACHABLE", "NOT_AN_IMAGE"])
    def test_other_camera_refusals_carry_no_block(self, bare, code):
        reply = self._snapshot(_Printer(refusal=CameraStreamError("The camera said no.", code=code)))
        assert reply["error"]["code"] == code
        assert camera_addon.RESULT_KEY not in reply


# ---------------------------------------------------------------------------
# webcam_stream
# ---------------------------------------------------------------------------


class TestTheLiveVideoRefusal:
    """``webcam_stream(action="start")`` refused, with the add-on missing and with it in place."""

    def _start(self, printer: _Printer) -> dict[str, Any]:
        from kiln import server

        with mock.patch.object(server, "_get_adapter", return_value=printer), mock.patch.object(
            server, "_video_route_block_for", return_value=None
        ):
            return server.webcam_stream(action="start")

    def test_a_refusal_while_the_addon_is_missing_carries_the_block(self, bare):
        reply = self._start(_Printer(needs=True))
        assert reply["success"] is False
        assert _plain(reply["error"]["message"]).startswith(camera_addon.WORDS["needed"])
        block = _addon_block(reply)
        assert block["status"] == "needed" and block["install"] == INSTALL

    def test_a_refusal_with_the_addon_in_place_carries_no_block(self, bare, tmp_path, monkeypatch):
        monkeypatch.setattr(camera_addon, "_system_program", lambda: str(tmp_path / "program"))
        reply = self._start(_Printer(needs=True))
        assert reply["success"] is False
        assert camera_addon.RESULT_KEY not in reply

    def test_a_camera_that_never_needs_it_carries_no_block(self, bare):
        reply = self._start(_Printer(needs=False))
        assert camera_addon.RESULT_KEY not in reply


# ---------------------------------------------------------------------------
# monitor_print and monitor_print_vision
# ---------------------------------------------------------------------------


def _reading_printer(printer: _Printer) -> mock.MagicMock:
    """*printer*'s camera behind an adapter that also answers a status read."""
    from kiln.printers.base import JobProgress, PrinterState, PrinterStatus

    adapter = mock.MagicMock()
    adapter.get_state.return_value = PrinterState(connected=True, state=PrinterStatus.PRINTING)
    adapter.get_job.return_value = JobProgress(file_name="part.gcode", completion=40.0)
    adapter.get_snapshot.side_effect = printer.get_snapshot
    adapter.camera_needs_addon.side_effect = printer.camera_needs_addon
    return adapter


class TestTheMonitorReport:
    """``monitor_print``'s ``Camera:`` line."""

    def _report(self, printer: _Printer) -> str:
        from kiln import server

        with mock.patch.object(server, "_get_adapter", return_value=_reading_printer(printer)):
            report = server.monitor_print(include_snapshot=True)
        assert isinstance(report, str), report
        return report

    def test_the_camera_line_is_the_sentence_alone(self, bare):
        report = self._report(_Printer(refusal=camera_addon.refusal()))
        line = next(ln for ln in report.splitlines() if ln.startswith("Camera:"))
        assert line == f"Camera: {camera_addon.WORDS['needed']}"
        assert "failed" not in line.lower()

    def test_any_other_camera_refusal_keeps_its_reason(self, bare):
        refused = CameraStreamError("The camera said no.", code="CAMERA_LIVEVIEW_OFF")
        report = self._report(_Printer(refusal=refused))
        assert "Camera: Snapshot capture failed: The camera said no." in report


class TestTheVisionReply:
    """``monitor_print_vision``'s ``snapshot`` block."""

    def _vision(self, printer: _Printer) -> dict[str, Any]:
        from kiln import server
        from kiln.plugins.monitoring_tools import _MonitoringToolsPlugin

        tools: dict[str, Any] = {}

        class _Catch:
            def tool(self, **_kwargs):
                def deco(fn):
                    tools[fn.__name__] = fn
                    return fn

                return deco

        _MonitoringToolsPlugin().register(_Catch())
        adapter = _reading_printer(printer)
        with mock.patch.object(server, "_get_adapter", return_value=adapter), mock.patch(
            "kiln.printers.base.adapter_has_camera", return_value=True
        ), mock.patch.object(server, "_check_auth", return_value=None):
            return tools["monitor_print_vision"](include_snapshot=True)

    def test_the_snapshot_block_carries_the_addon(self, bare):
        reply = self._vision(_Printer(refusal=camera_addon.refusal()))
        snapshot = reply["snapshot"]
        assert snapshot["available"] is False
        assert _plain(snapshot["reason"]) == camera_addon.WORDS["needed"]
        assert snapshot[camera_addon.RESULT_KEY]["install"] == INSTALL

    def test_any_other_failure_carries_no_block(self, bare):
        refused = CameraStreamError("The camera said no.", code="CAMERA_LIVEVIEW_OFF")
        snapshot = self._vision(_Printer(refusal=refused))["snapshot"]
        assert snapshot["reason"] == "The camera said no."
        assert camera_addon.RESULT_KEY not in snapshot


# ---------------------------------------------------------------------------
# Adding a printer: register_printer and the setup line every door shows
# ---------------------------------------------------------------------------


def _handshaking(needs: bool, answered: bool = True):
    """A printer with a camera handshake whose camera may need the add-on."""
    from kiln.printers.base import CameraHandshake

    class _Handshaking(_Printer):
        def camera_handshake(self) -> CameraHandshake:
            return CameraHandshake(answered, "printer_camera", "The camera answered.")

    return _Handshaking(needs=needs)


_TOOL_REST = 'Needs Kiln\'s free camera add-on (ask first, then upgrade_kiln addon="camera").'


class TestTheSetupLine:
    """``camera_check.camera_setup_line``: the handshake's line, then the add-on's, the label said once."""

    def test_the_tool_line_names_the_call_after_the_handshake(self, bare):
        from kiln import camera_check

        handshake, line = camera_check.camera_setup_line(_handshaking(needs=True), door="tool")
        assert handshake is not None and handshake.answered is True
        assert line == f"Camera: answering (built-in). {_TOOL_REST}"
        assert line.count("Camera:") == 1

    def test_the_terminal_line_carries_the_command(self, bare):
        from kiln import camera_check

        _handshake, line = camera_check.camera_setup_line(_handshaking(needs=True), door="cli")
        assert line == (
            "Camera: answering (built-in). Needs Kiln's free camera add-on. "
            f"Add it with: {camera_addon.command()}"
        )
        _plain(line.replace(camera_addon.command(), ""))

    def test_no_addon_sentence_when_the_program_is_here(self, bare, tmp_path, monkeypatch):
        from kiln import camera_check

        monkeypatch.setattr(camera_addon, "_system_program", lambda: str(tmp_path / "program"))
        assert camera_check.camera_setup_line(_handshaking(needs=True), door="tool")[1] == (
            "Camera: answering (built-in)."
        )

    def test_no_addon_sentence_for_a_camera_that_never_needs_it(self, bare):
        from kiln import camera_check

        assert camera_check.camera_setup_line(_handshaking(needs=False), door="cli")[1] == (
            "Camera: answering (built-in)."
        )

    def test_a_printer_without_the_handshake_still_hears_about_the_addon(self, bare):
        from kiln import camera_check

        assert camera_check.camera_setup_line(_Printer(needs=True), door="tool") == (
            None,
            camera_addon.WORDS["tool_line"],
        )

    def test_a_managed_install_names_its_own_program(self, bare, monkeypatch):
        from kiln import camera_check

        monkeypatch.setenv("KILN_UPDATE_COMMAND", MANAGED)
        line = camera_check.camera_setup_line(_handshaking(needs=True), door="cli")[1]
        assert line.startswith("Camera: answering (built-in). This copy of Kiln is looked after")
        assert line.endswith(MANAGED)
        assert line.count("Camera:") == 1


@pytest.fixture
def register(monkeypatch, tmp_path):
    """``register_printer`` for a Moonraker printer that answers, with its camera's need stubbed."""
    import requests

    from kiln import bench, server
    from kiln.printers.base import CameraHandshake
    from kiln.printers.moonraker import MoonrakerAdapter

    monkeypatch.setattr(server, "_check_auth", lambda *_a, **_k: None)
    monkeypatch.setattr("kiln.cli.config.get_config_path", lambda: tmp_path / "config.yaml")
    monkeypatch.setattr(bench, "offer_after_registration", lambda _adapter, name: None)
    monkeypatch.setattr(requests, "head", lambda *_a, **_k: object())
    monkeypatch.setattr(server, "read_machine_identity_bounded", lambda _read: None)
    monkeypatch.setattr(
        MoonrakerAdapter,
        "camera_handshake",
        lambda self: CameraHandshake(True, "moonraker_webcam_list", "The camera answered."),
    )

    def call(*, needs: bool) -> dict[str, Any]:
        monkeypatch.setattr(MoonrakerAdapter, "camera_needs_addon", lambda self: needs, raising=False)
        return server.register_printer(
            name="garage-printer", printer_type="moonraker", host="192.0.2.21",
            printer_model="neptune4_plus", printer_make="Elegoo",
            persist=False, verify_connection=False,
        )

    return call


class TestRegisterPrinter:
    """``register_printer``'s reply when the new printer's camera needs the add-on, and when it does not."""

    def test_the_reply_carries_the_setup_block_and_the_line(self, bare, register):
        result = register(needs=True)
        assert result["success"] is True, result
        block = _addon_block(result)
        assert block["status"] == "needed"
        assert block["message"] == camera_addon.WORDS["setup"].format(printer="garage-printer")
        assert f"{block['message']} {block['ask']}" == (
            "garage-printer is ready. Kiln needs one free add-on to show its camera. "
            "Want me to add it now?"
        )
        assert block["install"] == INSTALL
        assert result["camera_check"]["line"] == f"Camera: answering (built-in). {_TOOL_REST}"
        assert "add-on" not in result["message"]

    def test_a_camera_that_never_needs_it_gets_neither(self, bare, register):
        result = register(needs=False)
        assert result["success"] is True, result
        assert camera_addon.RESULT_KEY not in result
        assert result["camera_check"]["line"] == "Camera: answering (built-in)."

    def test_a_computer_that_has_the_program_gets_neither(self, bare, register, tmp_path, monkeypatch):
        monkeypatch.setattr(camera_addon, "_system_program", lambda: str(tmp_path / "program"))
        result = register(needs=True)
        assert result["success"] is True, result
        assert camera_addon.RESULT_KEY not in result
        assert result["camera_check"]["line"] == "Camera: answering (built-in)."
