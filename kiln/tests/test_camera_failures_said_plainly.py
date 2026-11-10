"""Every camera failure is said to a person in plain words, its specifics kept apart for an agent.

Each case drives one way a camera gives no picture through the real code (a
fake printer camera, a fake web camera, a scripted camera socket, a stubbed
stream program) and checks what a person reads: no program, protocol, port or
address, no tool call, one or two sentences.  The specifics an agent needs
(which address, what answered, the exit code, the exception) ride in
``detail``, and the error's code is the one it always was.  Then the doors:
``printer_snapshot`` hands ``detail`` beside ``error``, and live video says
why it cannot start in the same plain words.
"""

from __future__ import annotations

import contextlib
import json
import re
import socket
import struct
import subprocess
import threading
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

import pytest

from kiln.printers import bambu as bambu_mod
from kiln.printers import base as base_mod
from kiln.printers.bambu import BambuAdapter
from kiln.printers.base import (
    ExternalCamera,
    PrinterAdapter,
    PrinterCapabilities,
    PrinterError,
    fetch_external_snapshot,
)
from tests._fake_bambu_camera import FakeBambuCamera, make_jpeg
from tests.test_camera_addon import BANNED

ACCESS_CODE = "12345678"
#: A serial prefix Kiln has no model for, so only what a case sets decides.
UNPLACED = "ZZZ00A000000001"
NOT_A_PICTURE = b"\x00\x00\x00\x00\x01\x00\x00\x00"
SECRET_STREAM = "rtsp://viewer:hunter2@192.0.2.7:554/live"
REFUSED = subprocess.CompletedProcess(args=["ffmpeg"], returncode=1, stdout=b"")

ADDRESS = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b|localhost")
#: A name from code (a tool, a parameter, a setting key, an exception) or a call.
CODE_NAME = re.compile(r"\b[a-z]+_[a-z0-9_]+\b|\b\w+(?:Error|Exception)\b|`|\(\)")
SENTENCE_END = re.compile(r"[.!?](?=\s|$)")


def _plain(text: str) -> str:
    """*text*, checked as words for a person."""
    lowered = text.lower()
    assert [word for word in BANNED if word in lowered] == [], text
    assert not ADDRESS.search(text), text
    assert not CODE_NAME.search(text), text
    assert 1 <= len(SENTENCE_END.findall(text)) <= 2, text
    return text


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


class _Feed:
    """A camera socket that plays *script*: bytes to hand back, or an exception to raise."""

    def __init__(self, *script: bytes | BaseException) -> None:
        self._script = list(script)

    def settimeout(self, _seconds: float) -> None:
        pass

    def sendall(self, _data: bytes) -> None:
        pass

    def close(self) -> None:
        pass

    def recv(self, _size: int) -> bytes:
        step = self._script.pop(0) if self._script else b""
        if isinstance(step, BaseException):
            raise step
        return step


def _bambu(model: str | None) -> BambuAdapter:
    return BambuAdapter(
        host="127.0.0.1", access_code=ACCESS_CODE, serial=UNPLACED, timeout=2, printer_model=model,
    )


def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.contextmanager
def _web_camera(body: bytes, content_type: str) -> Iterator[str]:
    """A camera on this computer that answers every request with *body*; yields its address."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/camera"
    finally:
        server.shutdown()


def _raised(call: Callable[[], object]) -> PrinterError:
    with pytest.raises(PrinterError) as caught:
        call()
    return caught.value


# --- a camera the person registered --------------------------------------------


def _registered_stream(run: mock.Mock) -> PrinterError:
    with mock.patch.object(base_mod, "find_ffmpeg", return_value="/usr/bin/ffmpeg"), mock.patch.object(
        base_mod.subprocess, "run", run
    ):
        return _raised(lambda: fetch_external_snapshot(ExternalCamera(stream_url=SECRET_STREAM)))


def _registered_snapshot(body: bytes, content_type: str = "image/jpeg") -> PrinterError:
    with _web_camera(body, content_type) as url:
        return _raised(lambda: fetch_external_snapshot(ExternalCamera(snapshot_url=url)))


def _registered_video_with_no_picture() -> PrinterError:
    with _web_camera(b"--frame\r\nnot a picture\r\n", "multipart/x-mixed-replace; boundary=frame") as url:
        return _raised(lambda: fetch_external_snapshot(ExternalCamera(stream_url=url)))


def _registered_snapshot_nobody_answers() -> PrinterError:
    url = f"http://127.0.0.1:{_closed_port()}/camera"
    return _raised(lambda: fetch_external_snapshot(ExternalCamera(snapshot_url=url)))


# --- a Bambu printer's own camera -----------------------------------------------


def _port6000(model: str | None = "bambu_a1", **camera: object) -> PrinterError:
    with FakeBambuCamera(**camera) as server, mock.patch.object(bambu_mod, "_CAMERA_PORT", server.port):
        return _raised(_bambu(model).get_snapshot)


def _nothing_listening(model: str | None = "bambu_a1") -> PrinterError:
    with mock.patch.object(bambu_mod, "_CAMERA_PORT", _closed_port()):
        return _raised(_bambu(model).get_snapshot)


def _scripted(*script: bytes | BaseException) -> PrinterError:
    with mock.patch.object(bambu_mod, "_connect_camera_tls", lambda *_a, **_k: _Feed(*script)):
        return _raised(_bambu("bambu_a1").get_snapshot)


def _live_video_goes_quiet() -> PrinterError:
    frame = make_jpeg(1, size=500)
    header = struct.pack("<IIII", len(frame), 0, 1, 0)

    def watch() -> None:
        for _frame in _bambu("bambu_a1").camera_frame_source().frames(threading.Event()):
            pass

    with mock.patch.object(bambu_mod, "_connect_camera_tls", lambda *_a, **_k: _Feed(header + frame, TimeoutError())):
        return _raised(watch)


def _feed_ends_with_no_frame(model: str | None) -> PrinterError:
    with mock.patch.object(BambuAdapter, "_capture_jpeg_frame", lambda _self, **_k: None), mock.patch.object(
        bambu_mod, "_find_ffmpeg", return_value=None
    ):
        return _raised(_bambu(model).get_snapshot)


def _streaming_model(run: mock.Mock, **ipcam: object) -> PrinterError:
    adapter = _bambu("bambu_p2s")
    if ipcam:
        adapter._last_status["ipcam"] = ipcam
    with mock.patch.object(bambu_mod, "_find_ffmpeg", return_value="/usr/bin/ffmpeg"), mock.patch.object(
        base_mod.subprocess, "run", run
    ):
        return _raised(adapter.get_snapshot)


def _unplaced_both_feeds_fail() -> PrinterError:
    with mock.patch.object(bambu_mod, "_find_ffmpeg", return_value="/usr/bin/ffmpeg"), mock.patch.object(
        base_mod.subprocess, "run", mock.Mock(return_value=REFUSED)
    ):
        return _nothing_listening(model=None)


def _unplaced_no_addon_not_pictures() -> PrinterError:
    with mock.patch.object(bambu_mod, "_find_ffmpeg", return_value=None):
        return _port6000(model=None, frames=[NOT_A_PICTURE])


# --- every way a camera gives no picture ------------------------------------------

#: (case, the code it always had, specifics its detail carries).
CASES: list[tuple[str, Callable[[], PrinterError], str | None, tuple[str, ...]]] = [
    ("registered-stream-no-picture", lambda: _registered_stream(mock.Mock(return_value=REFUSED)), None,
     ("exit 1", "192.0.2.7:554")),
    ("registered-stream-too-slow",
     lambda: _registered_stream(mock.Mock(side_effect=subprocess.TimeoutExpired("ffmpeg", 5))), None,
     ("timed out", "192.0.2.7:554")),
    ("registered-stream-program-did-not-start",
     lambda: _registered_stream(mock.Mock(side_effect=FileNotFoundError(2, "No such file"))), None,
     ("FileNotFoundError",)),
    ("registered-snapshot-empty", lambda: _registered_snapshot(b""), None, ("127.0.0.1:", "no data")),
    ("registered-snapshot-web-page", lambda: _registered_snapshot(b"<html>log in</html>", "text/html"),
     "CAMERA_NOT_A_PICTURE", ("127.0.0.1:", "19 bytes")),
    ("registered-snapshot-nobody-answers", _registered_snapshot_nobody_answers, None,
     ("127.0.0.1:", "URLError")),
    ("registered-video-no-picture", _registered_video_with_no_picture, None, ("127.0.0.1:", "no JPEG frame")),
    ("own-camera-eight-bytes", lambda: _raised(_Camera(NOT_A_PICTURE).get_snapshot), "CAMERA_NOT_A_PICTURE",
     ("8 bytes",)),
    ("own-camera-empty", lambda: _raised(_Camera(b"").get_snapshot), "CAMERA_NOT_A_PICTURE", ("0 bytes",)),
    ("bambu-nothing-listening", _nothing_listening, "CAMERA_UNREACHABLE",
     ("127.0.0.1:", "ConnectionRefusedError")),
    ("bambu-no-secure-connection", lambda: _port6000(video_enabled=False), "CAMERA_UNREACHABLE",
     ("127.0.0.1:", "TLS")),
    ("bambu-wrong-access-code", lambda: _port6000(access_code="87654321"), "CAMERA_REFUSED",
     ("access code",)),
    ("bambu-answers-not-pictures", lambda: _port6000(frames=[NOT_A_PICTURE]), "CAMERA_NOT_A_PICTURE",
     ("8-byte",)),
    ("bambu-frame-header-of-nothing", lambda: _port6000(frames=[b""]), "CAMERA_UNREACHABLE", ("0 bytes",)),
    ("bambu-connected-then-silence", lambda: _scripted(TimeoutError()), "CAMERA_UNREACHABLE",
     ("no frame within 5s",)),
    ("bambu-connection-dropped", lambda: _scripted(ConnectionResetError(54, "Connection reset by peer")),
     "CAMERA_UNREACHABLE", ("ConnectionResetError",)),
    ("bambu-live-video-goes-quiet", _live_video_goes_quiet, "CAMERA_UNREACHABLE", ("30s",)),
    ("bambu-feed-ends-with-no-frame", lambda: _feed_ends_with_no_frame("bambu_a1"), "CAMERA_UNREACHABLE",
     ("ended with no frame",)),
    ("bambu-stream-no-picture", lambda: _streaming_model(mock.Mock(return_value=REFUSED)),
     "CAMERA_UNREACHABLE", ("exit 1", "127.0.0.1:322")),
    ("bambu-stream-too-slow",
     lambda: _streaming_model(mock.Mock(side_effect=subprocess.TimeoutExpired("ffmpeg", 5))),
     "CAMERA_UNREACHABLE", ("timed out",)),
    ("bambu-stream-program-did-not-start",
     lambda: _streaming_model(mock.Mock(side_effect=OSError(8, "Exec format error"))),
     "CAMERA_UNREACHABLE", ("OSError",)),
    ("bambu-liveview-off", lambda: _streaming_model(mock.Mock(return_value=REFUSED), rtsp_url="disable"),
     "CAMERA_LIVEVIEW_OFF", ("exit 1",)),
    ("bambu-unplaced-both-feeds-fail", _unplaced_both_feeds_fail, "CAMERA_UNREACHABLE",
     ("ConnectionRefusedError", "exit 1")),
    ("bambu-unplaced-no-addon-not-pictures", _unplaced_no_addon_not_pictures, "CAMERA_NOT_A_PICTURE",
     ("8-byte",)),
    ("bambu-unplaced-no-addon-nothing", lambda: _feed_ends_with_no_frame(None), "CAMERA_UNREACHABLE",
     ("ended with no frame",)),
]


@pytest.mark.parametrize(("make", "code", "specifics"), [c[1:] for c in CASES], ids=[c[0] for c in CASES])
def test_every_camera_failure_is_said_plainly_with_its_specifics_apart(make, code, specifics):
    exc = make()
    _plain(str(exc))
    assert getattr(exc, "code", None) == code
    for specific in specifics:
        assert specific in exc.detail, (specific, exc.detail)
    assert "hunter2" not in exc.detail and ACCESS_CODE not in exc.detail, exc.detail


# --- the doors -----------------------------------------------------------------------


def test_the_snapshot_reply_hands_the_detail_beside_the_words(monkeypatch):
    from kiln import server

    adapter = _bambu("bambu_a1")
    monkeypatch.setattr(server, "_get_adapter", lambda: adapter)
    with mock.patch.object(bambu_mod, "_CAMERA_PORT", _closed_port()):
        result = server.printer_snapshot()
    assert result["success"] is False, result
    assert result["error"]["code"] == "CAMERA_UNREACHABLE"
    assert result["error"]["retryable"] is True
    assert set(result["error"]) == {"code", "message", "retryable"}
    _plain(result["error"]["message"])
    assert "ConnectionRefusedError" in result["detail"] and "127.0.0.1:" in result["detail"]


def test_a_registered_camera_that_fails_is_said_as_itself_not_as_a_missing_webcam(monkeypatch):
    from kiln import server

    printer = _Camera(None)
    printer.set_external_camera(stream_url=SECRET_STREAM)
    monkeypatch.setattr(server, "_get_adapter", lambda: printer)
    with mock.patch.object(base_mod, "find_ffmpeg", return_value="/usr/bin/ffmpeg"), mock.patch.object(
        base_mod.subprocess, "run", mock.Mock(return_value=REFUSED)
    ):
        result = server.printer_snapshot()
    assert result["success"] is False, result
    assert result["error"]["code"] == "ERROR"
    _plain(result["error"]["message"])
    assert "exit 1" in result["detail"]
    assert "hunter2" not in json.dumps(result)


def test_live_video_says_why_it_cannot_start_in_plain_words(monkeypatch):
    from kiln import server

    stills = _Camera(None)
    stills.set_external_camera(snapshot_url="http://192.0.2.7/snap.jpg")
    relayed = _Camera(None)
    relayed.set_external_camera(stream_url=SECRET_STREAM)

    class _StreamingPrinter(_Camera):
        def get_stream_url(self) -> str | None:
            return "rtsp://192.0.2.8:554/live"

    for adapter in (stills, relayed, _StreamingPrinter(None)):
        capability = adapter.stream_capability()
        assert capability.available is False
        _plain(capability.reason or "")

    monkeypatch.delenv("KILN_HOSTED_MULTITENANT", raising=False)
    monkeypatch.setattr(server, "_get_adapter", lambda: relayed)
    result = server.webcam_stream(action="start")
    assert result["error"]["code"] == "RTSP_NOT_PROXIED"
    _plain(result["error"]["message"])
