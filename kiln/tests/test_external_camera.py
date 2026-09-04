"""A camera the user supplies reaches every frame reader, and its password reaches none.

Plenty of printers have no camera, or a poor one; the fix is a camera the
user points at the bed.  Before this, Kiln had no place to record one: the
Bambu adapter hard-coded its own RTSPS URL and Moonraker read the printer's
own webcam list, so a user with their own camera wired monitoring up outside
Kiln entirely.  Now every adapter's ``get_snapshot`` / ``get_stream_url`` is
wrapped at class creation to ask the user's camera first, so the doors that
already read frames keep calling what they always called, and a URL carrying
a password is redacted before it can appear in a reply or a log.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

import pytest

from kiln.printers.base import (
    EXTERNAL_CAMERA_NOTE,
    ExternalCamera,
    PrinterAdapter,
    PrinterCapabilities,
    PrinterError,
    adapter_has_camera,
    apply_external_camera,
    fetch_external_snapshot,
    redact_url_credentials,
    validate_external_camera_url,
)

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 300 + b"\xff\xd9"
SECRET_STREAM = "rtsp://viewer:hunter2@cam.local:554/live"


class _NoCameraPrinter(PrinterAdapter):
    """A printer with no camera of its own, recording whether it was asked."""

    def __init__(self) -> None:
        self.own_camera_asked = 0

    @property
    def name(self) -> str:
        return "no-camera"

    @property
    def capabilities(self) -> PrinterCapabilities:
        return PrinterCapabilities(can_snapshot=False)

    def get_snapshot(self) -> bytes | None:
        self.own_camera_asked += 1
        return None

    # The rest of the abstract surface, stubbed: nothing here prints.
    def _unused(self, *a, **k):  # noqa: ANN001, ANN202
        raise NotImplementedError

    for _method in PrinterAdapter.__abstractmethods__ - {"name", "capabilities"}:
        locals()[_method] = _unused
    del _method


class _OwnCameraPrinter(_NoCameraPrinter):
    @property
    def capabilities(self) -> PrinterCapabilities:
        return PrinterCapabilities(can_snapshot=True)

    def get_snapshot(self) -> bytes | None:
        self.own_camera_asked += 1
        return b"printer-frame"


@pytest.fixture
def still_server():
    """A local camera that answers one JPEG per request, counting hits."""
    hits: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            hits.append(self.path)
            if self.path.startswith("/mjpeg"):
                self.send_response(200)
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                self.end_headers()
                self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + JPEG + b"\r\n--frame\r\n")
                return
            if self.path.startswith("/empty"):
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.end_headers()
            self.wfile.write(JPEG)

        def log_message(self, *a):  # noqa: ANN002
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", hits
    finally:
        server.shutdown()


# --- redaction: the one thing that must never leak -------------------------


@pytest.mark.parametrize(
    ("url", "shown"),
    [
        (SECRET_STREAM, "rtsp://viewer:****@cam.local:554/live"),
        ("http://cam.local/snap.jpg", "http://cam.local/snap.jpg"),
        ("https://u:p@cam.local/?action=snapshot", "https://u:****@cam.local/?action=snapshot"),
        (None, None),
        ("", ""),
    ],
)
def test_credentials_are_masked_and_everything_else_survives(url, shown) -> None:
    assert redact_url_credentials(url) == shown


def test_describe_never_carries_the_password() -> None:
    camera = ExternalCamera(snapshot_url="http://a:b@cam/snap", stream_url=SECRET_STREAM)
    shown = json.dumps(camera.describe())
    assert "hunter2" not in shown and ":b@" not in shown
    assert camera.describe()["note"] == EXTERNAL_CAMERA_NOTE


def test_only_camera_schemes_are_accepted() -> None:
    assert validate_external_camera_url(" rtsps://cam/live ", what="x") == "rtsps://cam/live"
    with pytest.raises(ValueError, match="http\\(s\\) or rtsp\\(s\\)"):
        validate_external_camera_url("ftp://cam/snap", what="x")
    with pytest.raises(ValueError, match="host"):
        validate_external_camera_url("http://", what="x")
    with pytest.raises(ValueError) as excinfo:
        validate_external_camera_url("ftp://u:hunter2@cam/x", what="x")
    assert "hunter2" not in str(excinfo.value)


# --- the chokepoint ---------------------------------------------------------


def test_user_camera_is_preferred_and_the_printer_camera_is_not_asked(still_server) -> None:
    base, hits = still_server
    printer = _OwnCameraPrinter()
    printer.set_external_camera(snapshot_url=f"{base}/snap.jpg")

    assert printer.get_snapshot() == JPEG
    assert printer.own_camera_asked == 0
    assert hits == ["/snap.jpg"]
    assert printer.snapshot_source == "user_supplied"


def test_no_user_camera_means_the_printer_camera_as_before() -> None:
    printer = _OwnCameraPrinter()
    assert printer.get_snapshot() == b"printer-frame"
    assert printer.own_camera_asked == 1
    assert printer.snapshot_source == "printer"


def test_a_camera_less_printer_becomes_watchable(still_server) -> None:
    base, _ = still_server
    printer = _NoCameraPrinter()
    assert printer.has_camera is False and printer.snapshot_source is None
    printer.set_external_camera(snapshot_url=f"{base}/snap.jpg")
    assert printer.has_camera is True
    assert printer.get_snapshot() == JPEG
    printer.set_external_camera()
    assert printer.external_camera is None and printer.has_camera is False


def test_a_still_is_cut_from_an_mjpeg_stream_when_no_snapshot_url(still_server) -> None:
    base, hits = still_server
    printer = _NoCameraPrinter()
    printer.set_external_camera(stream_url=f"{base}/mjpeg")
    assert printer.get_snapshot() == JPEG
    assert printer.get_stream_url() == f"{base}/mjpeg"


def test_a_failing_user_camera_is_reported_not_replaced(still_server) -> None:
    """The printer's own camera would answer; the user's camera failing must
    be said as that, not papered over with a frame from a different view."""
    base, _ = still_server
    printer = _OwnCameraPrinter()
    printer.set_external_camera(snapshot_url=f"{base}/empty")
    with pytest.raises(PrinterError, match="answered with no image"):
        printer.get_snapshot()
    assert printer.own_camera_asked == 0

    printer.set_external_camera(snapshot_url="http://u:hunter2@127.0.0.1:9/snap")
    with pytest.raises(PrinterError) as excinfo:
        printer.get_snapshot()
    assert "did not answer" in str(excinfo.value)
    assert "hunter2" not in str(excinfo.value)
    assert "u:****@" in str(excinfo.value)


def test_rtsp_goes_through_ffmpeg_and_says_so_when_missing() -> None:
    printer = _NoCameraPrinter()
    printer.set_external_camera(stream_url=SECRET_STREAM)

    with mock.patch("kiln.printers.base.find_ffmpeg", return_value=None), pytest.raises(PrinterError) as excinfo:
        printer.get_snapshot()
    assert "ffmpeg" in str(excinfo.value) and "hunter2" not in str(excinfo.value)

    with mock.patch("kiln.printers.base.find_ffmpeg", return_value="/usr/bin/ffmpeg"), mock.patch(
        "kiln.printers.base.capture_rtsp_frame", return_value=JPEG
    ) as frame:
        assert printer.get_snapshot() == JPEG
    # The real URL, credentials and all, is what ffmpeg needs.
    assert frame.call_args.args[0] == SECRET_STREAM


def test_a_still_only_camera_has_no_stream_to_offer() -> None:
    class _StreamingPrinter(_OwnCameraPrinter):
        def get_stream_url(self) -> str | None:
            return "http://printer/webcam/?action=stream"

    printer = _StreamingPrinter()
    assert printer.get_stream_url() == "http://printer/webcam/?action=stream"
    printer.set_external_camera(snapshot_url="http://cam/snap.jpg")
    # Not the printer's own feed under the user's camera's name.
    assert printer.get_stream_url() is None


def test_config_entry_keys_reach_the_adapter() -> None:
    printer = _NoCameraPrinter()
    apply_external_camera(printer, {"host": "x"})
    assert printer.external_camera is None
    apply_external_camera(
        printer,
        {"camera_snapshot_url": " http://cam/snap ", "camera_stream_url": SECRET_STREAM},
    )
    assert printer.external_camera == ExternalCamera(
        snapshot_url="http://cam/snap", stream_url=SECRET_STREAM
    )


def test_fetch_refuses_an_empty_camera() -> None:
    with pytest.raises(PrinterError, match="No camera URL"):
        fetch_external_snapshot(ExternalCamera())


# --- the doors --------------------------------------------------------------


def test_saved_printer_round_trips_its_camera(tmp_path: Path) -> None:
    from kiln.cli.config import _read_config_file, save_printer

    path = tmp_path / "config.yaml"
    save_printer(
        "bench",
        "moonraker",
        "http://bench.local",
        camera_snapshot_url="http://cam.local/?action=snapshot",
        camera_stream_url=SECRET_STREAM,
        config_path=path,
    )
    entry = _read_config_file(path)["printers"]["bench"]
    assert entry["camera_snapshot_url"] == "http://cam.local/?action=snapshot"
    assert entry["camera_stream_url"] == SECRET_STREAM

    from kiln.cli.main import _make_adapter

    adapter = _make_adapter(dict(entry))
    assert adapter.external_camera == ExternalCamera(
        snapshot_url="http://cam.local/?action=snapshot", stream_url=SECRET_STREAM
    )


def test_server_config_entry_builder_carries_the_camera() -> None:
    from kiln import server

    adapter = server._build_adapter_from_config_entry(
        "bench",
        {
            "type": "moonraker",
            "host": "http://bench.local",
            "camera_snapshot_url": "http://cam.local/snap",
        },
    )
    assert adapter.external_camera == ExternalCamera(snapshot_url="http://cam.local/snap")


def test_register_printer_accepts_a_camera_and_never_echoes_its_password(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiln import server

    monkeypatch.setattr(server, "_check_auth", lambda *_a, **_k: None)
    monkeypatch.setattr("kiln.cli.config.get_config_path", lambda: tmp_path / "config.yaml")

    result = server.register_printer(
        name="bench",
        printer_type="moonraker",
        host="http://bench.local",
        camera_stream_url=SECRET_STREAM,
        verify_connection=False,
    )
    assert result["success"] is True, result
    assert result["camera"]["stream_url"] == "rtsp://viewer:****@cam.local:554/live"
    assert "hunter2" not in json.dumps(result)
    assert result["camera"]["note"] == EXTERNAL_CAMERA_NOTE
    assert server._get_registry().get("bench").external_camera.stream_url == SECRET_STREAM

    refused = server.register_printer(
        name="bench2",
        printer_type="moonraker",
        host="http://bench2.local",
        camera_snapshot_url="ftp://u:hunter2@cam/x",
        verify_connection=False,
    )
    assert refused["success"] is False and refused["error"]["code"] == "INVALID_ARGS"
    assert "hunter2" not in json.dumps(refused)


def test_printer_snapshot_tool_reads_the_user_camera(still_server, monkeypatch) -> None:
    from kiln import server

    base, hits = still_server
    printer = _OwnCameraPrinter()
    printer.set_external_camera(snapshot_url=f"{base}/snap.jpg")
    monkeypatch.setattr(server, "_get_adapter", lambda: printer)

    result = server.printer_snapshot()
    assert result["success"] is True, result
    assert result["camera_source"] == "user_supplied"
    assert result["size_bytes"] == len(JPEG)
    assert printer.own_camera_asked == 0


def test_webcam_stream_tool_says_rtsp_cannot_be_proxied(monkeypatch) -> None:
    from kiln import server

    printer = _OwnCameraPrinter()
    printer.set_external_camera(stream_url=SECRET_STREAM)
    monkeypatch.setattr(server, "_get_adapter", lambda: printer)

    result = server.webcam_stream(action="start")
    assert result["success"] is False
    assert result["error"]["code"] == "RTSP_NOT_PROXIED"
    assert "hunter2" not in json.dumps(result)


def test_an_adapters_own_override_is_still_camera_first(still_server) -> None:
    """The user's camera is honoured by an adapter that overrides get_snapshot
    and get_stream_url itself (every real backend does), not only by the
    base default — the wrap happens at class creation, so no door and no
    adapter has to remember it."""
    base, hits = still_server

    class _Backend(_OwnCameraPrinter):
        def get_snapshot(self) -> bytes | None:
            self.own_camera_asked += 1
            return b"backend-frame"

        def get_stream_url(self) -> str | None:
            return "http://printer/webcam/?action=stream"

    printer = _Backend()
    assert printer.get_snapshot() == b"backend-frame"
    printer.set_external_camera(snapshot_url=f"{base}/snap.jpg", stream_url=SECRET_STREAM)
    assert printer.get_snapshot() == JPEG
    assert printer.own_camera_asked == 1  # only the call before the camera was set
    assert printer.get_stream_url() == SECRET_STREAM
    assert adapter_has_camera(printer) is True


def test_has_camera_is_trusted_only_as_a_real_bool() -> None:
    """A mocked adapter must not read as camera-equipped by accident."""
    fake = mock.MagicMock()
    fake.capabilities.can_snapshot = False
    assert adapter_has_camera(fake) is False
    fake.capabilities.can_snapshot = True
    assert adapter_has_camera(fake) is True
    assert adapter_has_camera(object()) is False


def test_a_real_adapter_is_wrapped_and_falls_back_when_no_camera(still_server, monkeypatch) -> None:
    """The class-creation hook, pinned on a shipping adapter.

    Every earlier test builds its own subclass, which the hook also wraps —
    so if ``__init_subclass__`` stopped wrapping, those tests would still be
    exercising a class the hook never saw the way production does.  This one
    constructs a real backend the normal way (none of the eight adapters
    calls ``super().__init__()``), registers a camera, and asserts the
    backend's own snapshot code is not reached; then clears the camera and
    asserts the backend's own path runs.  Remove ``_wrap_camera_first`` from
    ``__init_subclass__`` and the first half fails.
    """
    from kiln.printers.moonraker import MoonrakerAdapter

    base, hits = still_server
    calls: list[str] = []

    def _no_webcams(self, path, **kwargs):  # noqa: ANN001, ANN202
        calls.append(path)
        return {"result": {"webcams": []}}

    monkeypatch.setattr(MoonrakerAdapter, "_get_json", _no_webcams)
    adapter = MoonrakerAdapter(host="http://127.0.0.1:9")
    assert getattr(MoonrakerAdapter.get_snapshot, "_kiln_camera_wrapped", False) is True

    adapter.set_external_camera(snapshot_url=f"{base}/snap.jpg", stream_url=f"{base}/mjpeg")
    assert adapter.get_snapshot() == JPEG
    assert adapter.get_stream_url() == f"{base}/mjpeg"
    assert calls == []  # Moonraker's own webcam list was never consulted

    adapter.set_external_camera()
    with pytest.raises(PrinterError, match="No webcams configured"):
        adapter.get_snapshot()
    assert calls == ["/server/webcams/list"]
