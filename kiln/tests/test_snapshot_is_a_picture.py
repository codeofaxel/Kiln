"""A camera answer that is not a picture is refused, never handed on as one.

A report from a Bambu P2S: ``printer_snapshot`` succeeded and returned
8 bytes that were not a JPEG.  The port-6000 reader passed on whatever
payload a frame header announced, and on printers whose camera streams over
RTSPS that port is not the camera.  Pinned here, against fake printer bytes:

* the check every adapter's snapshot passes through, the user's own camera
  included: JPEG and PNG pass, anything else is refused in words;
* the Bambu port-6000 reader refuses a payload that is not a picture;
* the camera transport comes from the printer's own status first, then its
  model, so a P2S never reads port 6000 as its camera;
* the door the report came through says what happened instead of succeeding.
"""

from __future__ import annotations

import struct
import threading
import zlib
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

import pytest

from kiln.printers import bambu as bambu_mod
from kiln.printers.bambu import BambuAdapter
from kiln.printers.base import (
    CameraStreamError,
    ExternalCamera,
    PrinterAdapter,
    PrinterCapabilities,
    PrinterError,
    fetch_external_snapshot,
)
from tests._fake_bambu_camera import FakeBambuCamera, make_jpeg

#: The size the report gave: a frame header announced 8 bytes and the
#: reader handed them back as the picture.
NOT_A_PICTURE = b"\x00\x00\x00\x00\x01\x00\x00\x00"
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 300 + b"\xff\xd9"
RTSP_JPEG = b"\xff\xd8\xff\xdb" + b"\x11" * 400 + b"\xff\xd9"


def _png() -> bytes:
    """A real 1x1 grey PNG."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(b"\x00\x80"))
        + chunk(b"IEND", b"")
    )


class _Camera(PrinterAdapter):
    """A printer whose own camera answers with ``frame``."""

    def __init__(self, frame: bytes | None) -> None:
        self.frame = frame

    @property
    def name(self) -> str:
        return "camera"

    @property
    def capabilities(self) -> PrinterCapabilities:
        return PrinterCapabilities(can_snapshot=True)

    def get_snapshot(self) -> bytes | None:
        return self.frame

    def _unused(self, *a, **k):  # noqa: ANN001, ANN202
        raise NotImplementedError

    for _method in PrinterAdapter.__abstractmethods__ - {"name", "capabilities"}:
        locals()[_method] = _unused
    del _method


def _bambu(server_port: int, **kwargs) -> BambuAdapter:
    defaults = {
        "host": "127.0.0.1",
        "access_code": "12345678",
        # A serial prefix Kiln has no model for, so only what a test sets
        # (model or the printer's own status) decides the transport.
        "serial": "ZZZ00A000000001",
        "timeout": 2,
        "printer_model": None,
    }
    defaults.update(kwargs)
    return BambuAdapter(**defaults)


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _ffmpeg_answers(frame: bytes):
    """Patch an installed ffmpeg that cuts *frame* from the RTSPS stream."""
    run = mock.MagicMock(return_value=mock.MagicMock(returncode=0, stdout=frame))
    return (
        mock.patch.object(bambu_mod, "_find_ffmpeg", return_value="/usr/bin/ffmpeg"),
        mock.patch("kiln.printers.base.subprocess.run", run),
        run,
    )


# ---------------------------------------------------------------------------
# The check every adapter's snapshot passes through
# ---------------------------------------------------------------------------


class TestEverySnapshotIsAPicture:
    def test_eight_bytes_from_a_printers_camera_are_refused_in_words(self):
        with pytest.raises(PrinterError) as caught:
            _Camera(NOT_A_PICTURE).get_snapshot()
        message = str(caught.value)
        assert "8 bytes" in message
        assert "not a picture" in message
        assert getattr(caught.value, "code", None) == "CAMERA_NOT_A_PICTURE"

    @pytest.mark.parametrize(
        "frame",
        [JPEG, _png(), make_jpeg(1), None],
        ids=["jpeg", "png", "port6000-jpeg", "no-camera"],
    )
    def test_a_picture_or_no_camera_passes_unchanged(self, frame):
        assert _Camera(frame).get_snapshot() == frame

    def test_a_jpeg_cut_short_is_not_a_picture(self):
        with pytest.raises(PrinterError, match="not a picture"):
            _Camera(JPEG[:-2]).get_snapshot()

    def test_a_users_camera_that_answers_with_a_web_page_is_refused(self):
        page = b"<html><body>Please log in</body></html>"

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(page)

            def log_message(self, *a):  # noqa: ANN002
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            camera = ExternalCamera(snapshot_url=f"http://127.0.0.1:{server.server_port}/snap")
            with pytest.raises(PrinterError, match="not a picture"):
                fetch_external_snapshot(camera)
        finally:
            server.shutdown()


# ---------------------------------------------------------------------------
# The Bambu port-6000 reader
# ---------------------------------------------------------------------------


class TestThePort6000Reader:
    def test_a_port_that_answers_with_eight_bytes_is_not_the_camera(self):
        with FakeBambuCamera(frames=[NOT_A_PICTURE]) as server:
            source = _bambu(server.port).camera_frame_source(port=server.port)
            stop = threading.Event()
            with pytest.raises(CameraStreamError) as caught:
                for _frame in source.frames(stop):
                    stop.set()
        assert caught.value.code == "CAMERA_NOT_A_PICTURE"
        assert "8 bytes" in str(caught.value)

    def test_real_frames_still_stream(self):
        frames = [make_jpeg(i, size=5000) for i in range(3)]
        with FakeBambuCamera(frames=frames, interval=0.01) as server:
            source = _bambu(server.port).camera_frame_source(port=server.port)
            stop = threading.Event()
            got: list[bytes] = []
            for frame in source.frames(stop):
                got.append(frame)
                if len(got) == 3:
                    stop.set()
        assert got == frames

    def test_a_snapshot_never_returns_the_eight_bytes(self):
        """Model unknown, so both feeds are tried: the 8 bytes are refused and
        the reply names both, never a success."""
        with FakeBambuCamera(frames=[NOT_A_PICTURE]) as server, mock.patch.object(
            bambu_mod, "_CAMERA_PORT", server.port
        ), mock.patch.object(bambu_mod, "_find_ffmpeg", return_value=None), pytest.raises(PrinterError) as caught:
            _bambu(server.port).get_snapshot()
        message = str(caught.value)
        assert "not a picture" in message
        assert "ffmpeg" in message


# ---------------------------------------------------------------------------
# The transport the printer names
# ---------------------------------------------------------------------------


class TestTheTransportThePrinterNames:
    def test_a_p2s_reads_its_camera_over_rtsps_and_never_port_6000(self):
        find, run_patch, run = _ffmpeg_answers(RTSP_JPEG)
        with FakeBambuCamera(frames=[NOT_A_PICTURE]) as server, mock.patch.object(
            bambu_mod, "_CAMERA_PORT", server.port
        ), find, run_patch:
            frame = _bambu(server.port, printer_model="bambu_p2s").get_snapshot()
            assert server.connections == 0
        assert frame == RTSP_JPEG
        assert "rtsps://bblp:12345678@127.0.0.1:322/streaming/live/1" in run.call_args[0][0]

    @pytest.mark.parametrize("model", ["bambu_h2d", "bambu_h2s", "bambu_x2d", "bambu_x1c"])
    def test_every_rtsps_model_skips_port_6000(self, model):
        find, run_patch, _run = _ffmpeg_answers(RTSP_JPEG)
        with FakeBambuCamera() as server, mock.patch.object(
            bambu_mod, "_CAMERA_PORT", server.port
        ), find, run_patch:
            assert _bambu(server.port, printer_model=model).get_snapshot() == RTSP_JPEG
            assert server.connections == 0

    def test_the_printers_own_status_outranks_its_model(self):
        """The model says port 6000; the printer says RTSPS.  The printer wins."""
        find, run_patch, _run = _ffmpeg_answers(RTSP_JPEG)
        with FakeBambuCamera() as server, mock.patch.object(
            bambu_mod, "_CAMERA_PORT", server.port
        ), find, run_patch:
            adapter = _bambu(server.port, printer_model="bambu_a1")
            adapter._last_status["ipcam"] = {"rtsp_url": "rtsps://192.168.1.5/streaming/live/1"}
            assert adapter.get_snapshot() == RTSP_JPEG
            assert server.connections == 0

    def test_a_printer_that_says_port_6000_is_read_there(self):
        frames = [make_jpeg(7)]
        with FakeBambuCamera(frames=frames) as server, mock.patch.object(
            bambu_mod, "_CAMERA_PORT", server.port
        ):
            adapter = _bambu(server.port, printer_model="bambu_p2s")
            adapter._last_status["ipcam"] = {"liveview": {"local": "local"}}
            assert adapter.get_snapshot() == frames[0]

    @pytest.mark.parametrize(
        "ipcam",
        [{"rtsp_url": "disable"}, {"liveview": {"local": "disabled"}}],
        ids=["rtsp_url", "liveview"],
    )
    def test_liveview_switched_off_is_said_plainly(self, ipcam):
        adapter = _bambu(_free_port(), printer_model="bambu_p2s")
        adapter._last_status["ipcam"] = ipcam
        with mock.patch.object(bambu_mod, "_find_ffmpeg", return_value="/usr/bin/ffmpeg"), mock.patch(
            "kiln.printers.base.subprocess.run"
        ) as run, pytest.raises(PrinterError, match="LAN Only Liveview is off"):
            adapter.get_snapshot()
        run.assert_not_called()

    def test_an_a1_reporting_liveview_off_still_reads_port_6000(self):
        """The liveview switch does not gate the port-6000 feed, so the
        report must not take away a camera that works."""
        frames = [make_jpeg(3)]
        with FakeBambuCamera(frames=frames) as server, mock.patch.object(
            bambu_mod, "_CAMERA_PORT", server.port
        ):
            adapter = _bambu(server.port, printer_model="bambu_a1")
            adapter._last_status["ipcam"] = {"rtsp_url": "disable"}
            assert adapter.get_snapshot() == frames[0]
            assert adapter.stream_capability().channel == "bambu_port6000"

    def test_a_p2s_is_not_offered_to_the_port_6000_relay(self):
        cap = _bambu(_free_port(), printer_model="bambu_p2s").stream_capability()
        assert cap.available is False
        assert cap.channel == "rtsp"

    def test_an_a1_still_streams_on_port_6000(self):
        cap = _bambu(_free_port(), printer_model="bambu_a1").stream_capability()
        assert cap.available is True
        assert cap.channel == "bambu_port6000"


# ---------------------------------------------------------------------------
# The door the report came through
# ---------------------------------------------------------------------------


class TestThePrinterSnapshotDoor:
    def test_it_says_not_a_picture_instead_of_succeeding(self, monkeypatch):
        from kiln import server

        monkeypatch.setattr(server, "_get_adapter", lambda: _Camera(NOT_A_PICTURE))
        result = server.printer_snapshot()
        assert result["success"] is False, result
        assert result["error"]["code"] == "CAMERA_NOT_A_PICTURE"
        assert "not a picture" in result["error"]["message"]
        assert "image_base64" not in result

    def test_a_real_picture_still_comes_back(self, monkeypatch):
        from kiln import server

        monkeypatch.setattr(server, "_get_adapter", lambda: _Camera(JPEG))
        result = server.printer_snapshot()
        assert result["success"] is True, result
        assert result["size_bytes"] == len(JPEG)
