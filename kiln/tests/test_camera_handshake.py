"""The camera handshake: does a camera answer, learned at setup without a frame.

Connecting is not using.  Pinned here: the handshake hangs up at a stream's
headers (a server that has not yet written a single frame byte is already
answered), the whole round is bounded in time, a printer type that knows no
address opens no socket, a camera that must be started to be asked is
reported as not checked rather than probed, the Bambu handshake completes
TLS and sends no access code, and every door that saves a printer shows the
one line.

Every server here listens on 127.0.0.1; nothing reaches the real network.
"""

from __future__ import annotations

import http.server
import socket
import threading
import time
from unittest import mock
from unittest.mock import MagicMock, patch

import pytest
import requests
from click.testing import CliRunner

from kiln import camera_check as cc
from kiln.printers.base import CameraHandshake, ExternalCamera, PrinterAdapter, PrinterState, PrinterStatus

_JPEG = b"\xff\xd8\xff\xe0" + b"0" * 64 + b"\xff\xd9"


# ---------------------------------------------------------------------------
# Servers: every one on loopback, none writes a frame until told to
# ---------------------------------------------------------------------------


class _GatedServer:
    """Answers GET with headers, then waits for ``release`` before any body.

    ``frames_written`` counts body writes, so a handshake that returns while
    it is still zero provably read no frame: there was none to read.
    """

    def __init__(self, content_type: str, *, require_header: str | None = None) -> None:
        self.release = threading.Event()
        self.frames_written = 0
        self.requests: list[dict[str, str]] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 — stdlib hook name
                outer.requests.append(dict(self.headers))
                if require_header and not self.headers.get(require_header):
                    self.send_error(401)
                    return
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                if content_type == "image/jpeg":
                    self.send_header("Content-Length", str(len(_JPEG)))
                self.end_headers()
                self.wfile.flush()
                if not outer.release.wait(5.0):
                    return
                try:
                    self.wfile.write(_JPEG)
                    self.wfile.flush()
                    outer.frames_written += 1
                except OSError:
                    pass

            def log_message(self, *args):
                pass

        self._httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"

    def __enter__(self) -> _GatedServer:
        threading.Thread(target=self._httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        return self

    def __exit__(self, *exc) -> None:
        self.release.set()
        self._httpd.shutdown()
        self._httpd.server_close()


class _SilentServer:
    """Accepts a connection and never answers: every address costs the full wait."""

    def __init__(self) -> None:
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(16)
        self.port = self._sock.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}/"

    def __enter__(self) -> _SilentServer:
        return self

    def __exit__(self, *exc) -> None:
        self._sock.close()


def _closed_port_url() -> str:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    return f"http://127.0.0.1:{port}/"


class _Stub:
    """The base adapter's handshake on a stand-in: no abstract methods to fill."""

    camera_handshake = PrinterAdapter.camera_handshake
    _own_camera_handshake = PrinterAdapter._own_camera_handshake
    _external_camera: ExternalCamera | None = None
    camera_check_ids: tuple[str, ...] = ()

    def __init__(self, probes=(), external: ExternalCamera | None = None) -> None:
        self._probes = list(probes)
        self.camera_check_ids = tuple(p.probe_id for p in self._probes)
        self._external_camera = external

    def camera_probes(self):
        return list(self._probes)


def _probe(probe_id: str, url: str) -> cc.CameraProbe:
    return cc.CameraProbe(probe_id, url, "a test basis")


# ---------------------------------------------------------------------------
# No frame is read, returned or stored
# ---------------------------------------------------------------------------


class TestTheHandshakeReadsNoFrame:
    def test_a_live_stream_is_answered_at_its_headers_before_any_frame_exists(self):
        with _GatedServer("multipart/x-mixed-replace; boundary=frame") as srv:
            started = time.monotonic()
            answered, what = cc.handshake_address(srv.url + "/?action=stream", timeout=2.0)
            elapsed = time.monotonic() - started
            assert (answered, what) == (True, "a live stream")
            assert srv.frames_written == 0  # the server had not written a frame yet
            assert elapsed < 1.5

    def test_a_still_is_answered_at_its_headers_before_its_bytes(self):
        with _GatedServer("image/jpeg") as srv:
            answered, what = cc.handshake_address(srv.url + "/snap", timeout=2.0)
            assert (answered, what) == (True, "an image")
            assert srv.frames_written == 0

    def test_the_result_carries_no_bytes_and_no_address(self):
        with _GatedServer("multipart/x-mixed-replace; boundary=frame") as srv:
            stub = _Stub([_probe("test_stream", srv.url + "/?action=stream")])
            handshake = stub.camera_handshake()
        assert handshake == CameraHandshake(
            True, "test_stream", "The printer's camera answered with a live stream at test_stream."
        )
        payload = handshake.to_dict()
        assert set(payload) == {"answered", "where", "detail"}
        assert all(isinstance(v, (str, bool, type(None))) for v in payload.values())
        assert "127.0.0.1" not in payload["detail"] and str(srv.port) not in payload["where"]

    def test_a_web_page_an_error_and_silence_are_not_a_camera(self):
        with _GatedServer("text/html") as page:
            assert cc.handshake_address(page.url + "/", timeout=2.0) == (False, "a web page, not a camera")
        with _GatedServer("image/jpeg", require_header="X-Api-Key") as locked:
            assert cc.handshake_address(locked.url + "/snap", timeout=2.0) == (False, "an error (HTTP 401)")
        assert cc.handshake_address(_closed_port_url(), timeout=2.0) == (False, "no reply")

    def test_the_whole_round_stays_inside_its_budget_and_says_what_went_untried(self):
        with _SilentServer() as silent:
            probes = [_probe(f"probe_{i}", silent.url) for i in range(4)]
            started = time.monotonic()
            handshake = cc.handshake_probes(probes, budget=0.5, address_timeout=0.2)
            elapsed = time.monotonic() - started
        assert elapsed < 1.5
        assert handshake.answered is False
        assert "went untried" in handshake.detail
        assert "no reply" in handshake.detail
        assert handshake.where.startswith("probe_0")
        assert "127.0.0.1" not in handshake.detail


# ---------------------------------------------------------------------------
# What each kind of camera answers
# ---------------------------------------------------------------------------


class TestWhatEachCameraAnswers:
    def test_a_type_with_no_address_answers_none_at_once_with_no_socket(self):
        stub = _Stub()
        with (
            mock.patch("socket.create_connection", side_effect=AssertionError("socket")),
            mock.patch("requests.Session.request", side_effect=AssertionError("network")),
        ):
            handshake = stub.camera_handshake()
        assert handshake == CameraHandshake(None, "", cc.NO_CAMERA_HANDSHAKE_DETAIL)

    def test_a_type_whose_printer_names_no_address_is_not_answering(self):
        stub = _Stub()
        stub.camera_check_ids = ("some_probe",)
        handshake = stub.camera_handshake()
        assert handshake.answered is False
        assert handshake.where == "some_probe"

    def test_the_users_camera_is_asked_in_place_of_the_printers_own(self):
        with _GatedServer("image/jpeg") as srv:
            stub = _Stub(
                [_probe("own_probe", _closed_port_url())],
                external=ExternalCamera(snapshot_url=srv.url + "/snap"),
            )
            handshake = stub.camera_handshake()
            assert srv.frames_written == 0
        assert handshake.answered is True
        assert handshake.where == "external"

    def test_a_users_camera_that_does_not_answer_is_false_never_a_refusal(self):
        camera = ExternalCamera(stream_url=_closed_port_url())
        handshake = cc.handshake_external_camera(camera, timeout=1.0)
        assert handshake == CameraHandshake(
            False,
            "external",
            "The camera you supplied is not answering (no reply); it may be off, or the address may be wrong.",
        )

    def test_a_users_rtsp_camera_is_not_checked_because_kiln_would_have_to_open_it(self):
        camera = ExternalCamera(stream_url="rtsp://user:secret@10.0.0.9:554/live")
        with mock.patch("socket.create_connection", side_effect=AssertionError("socket")):
            handshake = cc.handshake_external_camera(camera, timeout=1.0)
        assert handshake.answered is None
        assert handshake.where == "external"
        assert "secret" not in handshake.detail and "10.0.0.9" not in handshake.detail

    def test_elegoo_is_not_checked_because_asking_switches_video_on(self):
        from kiln.printers.elegoo import ElegooAdapter

        adapter = ElegooAdapter(host="192.0.2.50", mainboard_id="ABCD1234ABCD1234", timeout=2)
        with mock.patch.object(adapter, "_send_command", side_effect=AssertionError("command 386 sent")):
            handshake = adapter.camera_handshake()
        assert handshake.answered is None
        assert handshake.where == "elegoo_video_url"
        assert "386" in handshake.detail

    def test_prusalink_asks_its_snap_endpoint_with_its_key_and_reads_no_picture(self):
        from kiln.printers.prusalink import PrusaLinkAdapter

        with _GatedServer("image/jpeg", require_header="X-Api-Key") as srv:
            adapter = PrusaLinkAdapter(host=srv.url, api_key="k3y", timeout=2, retries=1)
            handshake = adapter.camera_handshake()
            assert srv.frames_written == 0
            assert srv.requests and srv.requests[0].get("X-Api-Key") == "k3y"
        assert handshake == CameraHandshake(
            True, "prusalink_cameras_snap", "The printer's camera answered with an image."
        )

    def test_octoprint_offers_its_webcam_address_without_contacting_the_printer(self):
        from kiln.printers.octoprint import OctoPrintAdapter

        adapter = OctoPrintAdapter(host="http://192.0.2.7", api_key="k")
        with mock.patch("requests.Session.request", side_effect=AssertionError("network")):
            probes = adapter.camera_probes()
        assert OctoPrintAdapter.camera_check_ids == ("octoprint_webcam_stream",)
        assert [(p.probe_id, p.url) for p in probes] == [
            ("octoprint_webcam_stream", "http://192.0.2.7/webcam/?action=stream")
        ]
        assert "action=stream" in probes[0].basis

    def test_moonraker_offers_the_address_its_own_webcam_list_names(self):
        from kiln.printers.moonraker import MoonrakerAdapter

        adapter = MoonrakerAdapter(host="http://192.0.2.8:7125", timeout=2, retries=1)
        listing = {"result": {"webcams": [{"stream_url": "/webcam/?action=stream"}]}}
        with mock.patch.object(adapter, "_get_json", return_value=listing):
            probes = adapter.camera_probes()
        assert MoonrakerAdapter.camera_check_ids == ("moonraker_webcam_list",)
        assert [(p.probe_id, p.url) for p in probes] == [
            ("moonraker_webcam_list", "http://192.0.2.8:7125/webcam/?action=stream")
        ]
        with mock.patch.object(adapter, "_get_json", return_value={"result": {"webcams": []}}):
            handshake = adapter.camera_handshake()
        assert handshake.answered is False
        assert handshake.where == "moonraker_webcam_list"


class TestBambu:
    """TLS completes, nothing is sent: the fake counts auth'd connections and frames."""

    @staticmethod
    def _adapter(model: str):
        from kiln.printers.bambu import BambuAdapter

        return BambuAdapter(
            host="127.0.0.1", access_code="12345678", serial="01P00A000000001",
            printer_model=model, tls_mode="insecure",
        )

    def test_a1_answers_after_tls_with_no_access_code_sent_and_no_video_started(self, monkeypatch):
        pytest.importorskip("cryptography")
        from kiln.printers import bambu
        from tests._fake_bambu_camera import FakeBambuCamera

        with FakeBambuCamera() as cam:
            monkeypatch.setattr(bambu, "_CAMERA_PORT", cam.port)
            handshake = self._adapter("bambu_a1").camera_handshake()
            time.sleep(0.1)
            assert (cam.connections, cam.rejected, cam.frames_sent) == (0, 0, 0)
        assert handshake.answered is True
        assert handshake.where == "bambu_port6000"
        assert "access code" in handshake.detail
        assert "127.0.0.1" not in handshake.detail

    def test_a1_with_video_off_is_not_answering(self, monkeypatch):
        pytest.importorskip("cryptography")
        from kiln.printers import bambu
        from tests._fake_bambu_camera import FakeBambuCamera

        with FakeBambuCamera(video_enabled=False) as cam:
            monkeypatch.setattr(bambu, "_CAMERA_PORT", cam.port)
            handshake = self._adapter("bambu_a1").camera_handshake()
        assert handshake.answered is False
        assert handshake.where == "bambu_port6000"
        assert "127.0.0.1" not in handshake.detail and str(cam.port) not in handshake.detail

    def test_x1_asks_the_rtsps_port_and_stops_at_tls(self, monkeypatch):
        pytest.importorskip("cryptography")
        from kiln.printers import bambu
        from tests._fake_bambu_camera import FakeBambuCamera

        with FakeBambuCamera() as cam:
            monkeypatch.setattr(bambu, "_RTSPS_PORT", cam.port)
            monkeypatch.setattr(bambu, "_CAMERA_PORT", 1)  # not this one
            handshake = self._adapter("bambu_x1c").camera_handshake()
            assert cam.connections == 0
        assert handshake.answered is True
        assert handshake.where == "bambu_rtsps_322"
        assert "RTSP" in handshake.detail

    def test_the_relay_feed_still_authenticates_through_the_shared_connect(self, monkeypatch):
        pytest.importorskip("cryptography")

        from tests._fake_bambu_camera import FakeBambuCamera

        with FakeBambuCamera() as cam:
            source = self._adapter("bambu_a1").camera_frame_source(port=cam.port, timeout=3.0)
            stop = threading.Event()
            frame = next(iter(source.frames(stop)))
            stop.set()
            assert frame.startswith(b"\xff\xd8")
            assert cam.connections == 1


# ---------------------------------------------------------------------------
# The line every door shows
# ---------------------------------------------------------------------------


class _Fake:
    external_camera = None

    def __init__(self, handshake: CameraHandshake) -> None:
        self._handshake = handshake
        self.get_state = MagicMock(
            return_value=PrinterState(
                state=PrinterStatus.IDLE, connected=True,
                tool_temp_actual=22.0, tool_temp_target=0.0, bed_temp_actual=21.0, bed_temp_target=0.0,
            )
        )

    def camera_handshake(self) -> CameraHandshake:
        return self._handshake


_ANSWERING = CameraHandshake(True, "octoprint_webcam_stream", "The printer's camera answered with a live stream.")
_SILENT = CameraHandshake(False, "creality_fluidd_webcam,creality_mjpeg_8080", "Nothing answered as a camera (...).")
_UNCHECKABLE = CameraHandshake(None, "elegoo_video_url", "This printer names its camera only when video is on.")


class TestTheLine:
    @pytest.mark.parametrize(
        ("handshake", "door", "line"),
        [
            (_ANSWERING, "cli", "Camera: answering (built-in)."),
            (CameraHandshake(True, "external", "x"), "tool", "Camera: answering (the camera you supplied)."),
            (
                _SILENT, "tool",
                "Camera: not answering at creality_fluidd_webcam,creality_mjpeg_8080; "
                "a camera you point at the bed can be added with camera_stream_url.",
            ),
            (
                _SILENT, "cli",
                "Camera: not answering at creality_fluidd_webcam,creality_mjpeg_8080; "
                "a camera you point at the bed can be added with kiln auth --camera-stream-url.",
            ),
            (
                _UNCHECKABLE, "cli",
                "Camera: Kiln cannot check this printer's camera without starting it. "
                "This printer names its camera only when video is on.",
            ),
            (
                CameraHandshake(None, "", cc.NO_CAMERA_HANDSHAKE_DETAIL), "tool",
                f"Camera: {cc.NO_CAMERA_HANDSHAKE_DETAIL} If it has one, "
                "a camera you point at the bed can be added with camera_stream_url.",
            ),
            (
                CameraHandshake(False, "external", "The camera you supplied is not answering (no reply)."), "cli",
                "Camera: The camera you supplied is not answering (no reply). Kept anyway.",
            ),
        ],
    )
    def test_each_outcome_reads_as_one_line(self, handshake, door, line):
        assert cc.describe_handshake(handshake, door=door) == line

    def test_a_stand_in_without_the_handshake_shows_nothing(self):
        assert cc.camera_setup_line(MagicMock(), door="cli") == (None, "")
        assert cc.camera_setup_line(object(), door="cli") == (None, "")

    def test_a_handshake_that_raises_is_not_checked_not_a_failed_setup(self):
        adapter = MagicMock()
        adapter.camera_handshake.side_effect = RuntimeError("boom")
        handshake, line = cc.camera_setup_line(adapter, door="cli")
        assert handshake is None
        assert line.startswith("Camera: not checked")


# ---------------------------------------------------------------------------
# Every door that saves a printer
# ---------------------------------------------------------------------------


@pytest.fixture
def register(monkeypatch, tmp_path):
    from kiln import bench, server
    from kiln.printers.moonraker import MoonrakerAdapter

    monkeypatch.setattr(server, "_check_auth", lambda *_a, **_k: None)
    monkeypatch.setattr("kiln.cli.config.get_config_path", lambda: tmp_path / "config.yaml")
    monkeypatch.setattr(bench, "offer_after_registration", lambda _adapter, name: None)
    calls: list[str] = []

    def call(*, answers: bool, handshake: CameraHandshake, **kwargs) -> dict:
        def head(url, **_kw):
            if not answers:
                raise requests.ConnectionError(f"no route to {url}")
            return object()

        monkeypatch.setattr(requests, "head", head)

        def fake_handshake(self):
            calls.append("handshake")
            return handshake

        monkeypatch.setattr(MoonrakerAdapter, "camera_handshake", fake_handshake)
        return server.register_printer(
            name="far", printer_type="moonraker", host="192.0.2.21", printer_model="neptune4_plus",
            persist=False, verify_connection=False, **kwargs,
        )

    return call, calls


class TestRegisterPrinter:
    def test_the_reply_says_whether_the_camera_answered(self, register):
        call, calls = register
        result = call(answers=True, handshake=_ANSWERING)
        assert result["success"], result
        assert result["camera_check"]["answered"] is True
        assert result["camera_check"]["where"] == "octoprint_webcam_stream"
        assert result["camera_check"]["line"] == "Camera: answering (built-in)."
        assert calls == ["handshake"]

    def test_a_printer_that_did_not_answer_is_not_asked_about_its_camera(self, register):
        call, calls = register
        result = call(answers=False, handshake=_ANSWERING)
        assert result["success"], result
        assert "camera_check" not in result
        assert calls == []

    def test_a_users_camera_that_does_not_answer_is_a_warning_not_a_refusal(self, register):
        call, calls = register
        silent = CameraHandshake(False, "external", "The camera you supplied is not answering (no reply).")
        result = call(answers=False, handshake=silent, camera_stream_url="http://cam.local/stream")
        assert result["success"], result
        assert calls == ["handshake"]  # the user's camera is on its own host: asked even then
        assert result["camera_check"]["answered"] is False
        assert "Camera: The camera you supplied is not answering (no reply). Kept anyway." in result["warnings"]


class TestKilnAuth:
    def test_auth_shows_the_camera_line_after_saving(self, monkeypatch, tmp_path):
        from kiln.cli.main import cli

        monkeypatch.setattr("kiln.cli.config.get_config_path", lambda: tmp_path / "config.yaml")
        with patch("kiln.cli.main._make_adapter", return_value=_Fake(_SILENT)):
            result = CliRunner().invoke(
                cli, ["auth", "--name", "k1", "--host", "http://192.0.2.9", "--type", "moonraker"]
            )
        assert result.exit_code == 0, result.output
        assert (
            "Camera: not answering at creality_fluidd_webcam,creality_mjpeg_8080; "
            "a camera you point at the bed can be added with kiln auth --camera-stream-url."
        ) in result.output

    def test_auth_json_carries_the_handshake(self, monkeypatch, tmp_path):
        import json

        from kiln.cli.main import cli

        monkeypatch.setattr("kiln.cli.config.get_config_path", lambda: tmp_path / "config.yaml")
        with patch("kiln.cli.main._make_adapter", return_value=_Fake(_ANSWERING)):
            result = CliRunner().invoke(
                cli, ["auth", "--name", "k1", "--host", "http://192.0.2.9", "--type", "moonraker", "--json"]
            )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["data"]["camera_check"]["answered"] is True
        assert payload["data"]["camera_check"]["line"] == "Camera: answering (built-in)."


class TestKilnSetup:
    def test_setup_shows_the_camera_line_after_connecting(self, monkeypatch, tmp_path):
        from kiln.cli.main import cli
        from kiln.discovery import DiscoveredPrinter

        monkeypatch.setattr("kiln.cli.config.get_config_path", lambda: tmp_path / "config.yaml")
        found = DiscoveredPrinter(host="192.168.1.9", port=7125, printer_type="moonraker", name="K1")
        with (
            patch("kiln.terms.is_current", return_value=True),
            patch("kiln.cli.discovery.discover_printers", return_value=[found]),
            patch("kiln.cli.printer_model_prompt.prompt_for_printer_model", return_value="k1c"),
            patch("kiln.cli.main._make_adapter", return_value=_Fake(_UNCHECKABLE)),
        ):
            result = CliRunner().invoke(cli, ["setup"], input="1\nk1\n\n" + "\n" * 8)
        assert "Connected!" in result.output, result.output
        assert "  Camera: Kiln cannot check this printer's camera without starting it." in result.output


class TestQuickstart:
    @patch("kiln.cli.discovery.discover_printers", return_value=[])
    @patch("kiln.cli.main._list_printers", return_value=[
        {"name": "ender3", "type": "octoprint", "host": "http://ender.local", "active": True},
    ])
    @patch("kiln.cli.main.load_printer_config", return_value={"type": "octoprint", "host": "http://ender.local"})
    @patch("kiln.cli.main._make_adapter")
    def test_quickstart_status_carries_the_camera_line(self, mock_make, _cfg, _list, _disc) -> None:
        import json

        from kiln.cli.main import cli

        mock_make.return_value = _Fake(_ANSWERING)
        result = CliRunner().invoke(cli, ["quickstart"])
        assert "    Camera: answering (built-in)." in result.output, result.output
        result = CliRunner().invoke(cli, ["quickstart", "--json"])
        data = json.loads(result.output)["data"]
        assert data["status"]["camera_check"]["answered"] is True


class TestBridgeFirstPrinter:
    def test_the_found_printer_gets_its_camera_line(self, monkeypatch, capsys):
        import click as _click

        import kiln.cli.bridge_commands as bcmd
        import kiln.cli.config as cfg
        import kiln.cli.discovery as disc
        import kiln.cli.printer_model_prompt as pmp
        from kiln.discovery import DiscoveredPrinter

        monkeypatch.setattr(cfg, "list_printers", lambda **kw: [])
        monkeypatch.setattr(cfg, "save_printer", lambda *a, **kw: "/tmp/config.yaml")
        monkeypatch.setattr(cfg, "load_printer_config", lambda name: {"type": "moonraker", "host": "x"})
        monkeypatch.setattr("kiln.cli.main._make_adapter", lambda _cfg: _Fake(_ANSWERING))
        monkeypatch.setattr(bcmd, "_onboarding_interactive", lambda: True)
        found = DiscoveredPrinter(host="192.168.1.23", port=7125, printer_type="moonraker", name="K1C")
        monkeypatch.setattr(disc, "discover_printers", lambda *a, **kw: [found])
        monkeypatch.setattr(pmp, "prompt_for_printer_model", lambda *a, **kw: "k1c")
        monkeypatch.setattr(_click, "confirm", lambda *a, **kw: True)

        bcmd._offer_first_printer()

        out = capsys.readouterr().out
        assert "Saved ✓" in out
        assert "  Camera: answering (built-in)." in out
