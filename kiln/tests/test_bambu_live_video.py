"""Live video from a Bambu printer, through the relay Kiln already has.

Until this file, the port-6000 camera protocol lived in one place —
``BambuAdapter._capture_jpeg_frame`` — and did one thing: open a TLS
socket, authenticate, read the FIRST JPEG, and hang up.  The printer pushes
a continuous stream; Kiln took one frame per call and every viewer paid a
fresh printer connection.  The relay (``kiln.streaming.MJPEGProxy``) could
only re-serve an HTTP MJPEG URL, so a Bambu ``webcam_stream`` start handed
it a masked ``rtsps://`` string and it retried that forever.

What is pinned here, against a fake port-6000 server that speaks the
printer's own protocol (auth packet, then framed JPEGs, chunked like a real
TLS record stream):

* the still path is unchanged and routes through the SAME auth-packet and
  TLS-context helpers the stream source uses (one implementation, not two);
* a frame source that yields every frame in order, framed by the 16-byte
  header rather than by scanning for JPEG markers;
* honest refusals: a wrong access code and a closed port are named, never
  reported as "no frame";
* ONE authenticated printer connection fans out to every relay viewer, and
  no viewer that keeps up misses a frame;
* frame age and the measured frame rate ride the relay's status, so a
  frozen picture can never pass as live;
* the relay is local-only by design and says so on the hosted server.
"""

from __future__ import annotations

import http.client
import socket
import struct
import threading
import time
from unittest import mock

import pytest

from kiln.printers import bambu as bambu_mod
from kiln.printers.bambu import BambuAdapter
from tests._fake_bambu_camera import FakeBambuCamera, make_jpeg

ACCESS_CODE = "12345678"


def _adapter(server: FakeBambuCamera, **kwargs) -> BambuAdapter:
    defaults = {
        "host": "127.0.0.1",
        "access_code": server.access_code,
        "serial": "039ABC123",
        "timeout": 2,
        "printer_model": "bambu_a1",
    }
    defaults.update(kwargs)
    return BambuAdapter(**defaults)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _read_parts(port: int, count: int, timeout: float = 10.0) -> list[tuple[dict, bytes]]:
    """GET /stream and return the first *count* multipart parts as (headers, body)."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    conn.request("GET", "/stream")
    resp = conn.getresponse()
    assert resp.status == 200, resp.status
    assert resp.getheader("Content-Type", "").startswith("multipart/x-mixed-replace")
    parts: list[tuple[dict, bytes]] = []
    buf = b""
    deadline = time.monotonic() + timeout
    while len(parts) < count and time.monotonic() < deadline:
        chunk = resp.fp.read1(65536) if hasattr(resp.fp, "read1") else resp.fp.read(65536)
        if not chunk:
            break
        buf += chunk
        while True:
            start = buf.find(b"--kilnframe\r\n")
            if start == -1:
                break
            head_end = buf.find(b"\r\n\r\n", start)
            if head_end == -1:
                break
            headers: dict[str, str] = {}
            for line in buf[start + len(b"--kilnframe\r\n") : head_end].decode().split("\r\n"):
                k, _, v = line.partition(":")
                headers[k.strip().lower()] = v.strip()
            length = int(headers["content-length"])
            body_start = head_end + 4
            if len(buf) < body_start + length + 2:
                break
            parts.append((headers, buf[body_start : body_start + length]))
            buf = buf[body_start + length + 2 :]
            if len(parts) >= count:
                break
    conn.close()
    return parts


# ---------------------------------------------------------------------------
# The shared pieces: one auth packet, one TLS context, both paths use them
# ---------------------------------------------------------------------------


class TestTheSharedCameraHelpers:
    def test_the_auth_packet_is_the_printers_80_byte_layout(self):
        with FakeBambuCamera(access_code=ACCESS_CODE) as server:
            packet = _adapter(server)._camera_auth_packet()
        assert len(packet) == 80
        assert struct.unpack("<IIII", packet[:16]) == (0x40, 0x3000, 0, 0)
        assert packet[16:48] == b"bblp".ljust(32, b"\x00")
        assert packet[48:80] == ACCESS_CODE.encode("ascii").ljust(32, b"\x00")

    def test_the_still_path_returns_the_first_frame_exactly(self):
        frames = [make_jpeg(i, size=9000) for i in range(3)]
        with FakeBambuCamera(access_code=ACCESS_CODE, frames=frames) as server, mock.patch.object(
            bambu_mod, "_CAMERA_PORT", server.port
        ):
            frame = _adapter(server)._capture_jpeg_frame(timeout=5.0)
        # Framed by the header: the whole payload, not "up to the first
        # 0xFFD9 the scanner happened to see".
        assert frame == frames[0]

    def test_the_still_path_authenticates_through_the_shared_packet(self):
        """Mutation pin: break the shared helper and the still path breaks
        with it — proof the still path has no private copy of the packet."""
        with FakeBambuCamera(access_code=ACCESS_CODE) as server:
            adapter = _adapter(server)
            with mock.patch.object(bambu_mod, "_CAMERA_PORT", server.port), mock.patch.object(
                BambuAdapter, "_camera_auth_packet", return_value=b"\x00" * 80
            ):
                assert adapter._capture_jpeg_frame(timeout=2.0) is None
            assert server.rejected == 1


# ---------------------------------------------------------------------------
# The frame source: every frame, in order, honest about refusal
# ---------------------------------------------------------------------------


class TestThePort6000FrameSource:
    def test_it_yields_every_frame_in_order_across_chunk_boundaries(self):
        frames = [make_jpeg(i, size=7000 + 1500 * i) for i in range(4)]
        with FakeBambuCamera(access_code=ACCESS_CODE, frames=frames, interval=0.01) as server:
            source = _adapter(server).camera_frame_source(port=server.port)
            stop = threading.Event()
            got: list[bytes] = []
            for jpeg in source.frames(stop):
                got.append(jpeg)
                if len(got) == 6:
                    stop.set()
                    break
        assert got == frames + frames[:2]
        assert source.kind == "bambu_port6000"
        assert ACCESS_CODE not in source.label
        assert server.connections == 1

    def test_a_wrong_access_code_is_named_not_swallowed(self):
        from kiln.printers.base import CameraStreamError

        with FakeBambuCamera(access_code="87654321") as server:
            source = _adapter(server, access_code=ACCESS_CODE).camera_frame_source(port=server.port)
            with pytest.raises(CameraStreamError) as excinfo:
                next(iter(source.frames(threading.Event())))
        assert excinfo.value.code == "CAMERA_REFUSED"
        assert "access code" in str(excinfo.value)
        assert "liveview" in str(excinfo.value).lower()

    def test_a_dead_port_is_named_as_unreachable(self):
        from kiln.printers.base import CameraStreamError

        with FakeBambuCamera(access_code=ACCESS_CODE, video_enabled=False) as server:
            source = _adapter(server).camera_frame_source(port=server.port)
            with pytest.raises(CameraStreamError) as excinfo:
                next(iter(source.frames(threading.Event())))
        assert excinfo.value.code == "CAMERA_UNREACHABLE"
        assert "6000" in str(excinfo.value) or "liveview" in str(excinfo.value).lower()

    def test_a_closed_port_is_unreachable_too(self):
        from kiln.printers.base import CameraStreamError

        with FakeBambuCamera(access_code=ACCESS_CODE) as server:
            adapter = _adapter(server)
        # server is stopped: nothing listens on that port any more.
        source = adapter.camera_frame_source(port=server.port)
        with pytest.raises(CameraStreamError) as excinfo:
            next(iter(source.frames(threading.Event())))
        assert excinfo.value.code == "CAMERA_UNREACHABLE"


# ---------------------------------------------------------------------------
# The relay: one printer connection, every viewer, every frame
# ---------------------------------------------------------------------------


class TestTheRelayFansOut:
    def test_one_printer_connection_serves_three_viewers_without_a_missed_frame(self):
        from kiln.streaming import MJPEGProxy

        frames = [make_jpeg(i, size=5000) for i in range(8)]
        with FakeBambuCamera(access_code=ACCESS_CODE, frames=frames, interval=0.05) as server:
            source = _adapter(server).camera_frame_source(port=server.port)
            proxy = MJPEGProxy()
            port = _free_port()
            info = proxy.start(frame_source=source, port=port, printer_name="a1")
            try:
                assert info.active is True
                assert info.source_kind == "bambu_port6000"
                assert ACCESS_CODE not in (info.source_url or "")
                results: list[list[tuple[dict, bytes]]] = [[], [], []]

                def viewer(i: int) -> None:
                    results[i] = _read_parts(port, 4)

                threads = [threading.Thread(target=viewer, args=(i,)) for i in range(3)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join(timeout=15)
                for parts in results:
                    assert len(parts) == 4, len(parts)
                    seqs = [int(h["x-frame-sequence"]) for h, _ in parts]
                    # A viewer that keeps up sees consecutive frames: fan-out
                    # is by sequence, never "whoever cleared the event first".
                    assert seqs == list(range(seqs[0], seqs[0] + 4)), seqs
                    for h, body in parts:
                        assert h["content-type"] == "image/jpeg"
                        assert body in frames
                        assert float(h["x-frame-age-seconds"]) < 2.0
                # A viewer's departure is noticed at the next write.
                deadline = time.monotonic() + 3
                while proxy.status().connected_clients and time.monotonic() < deadline:
                    time.sleep(0.05)
                status = proxy.status()
                assert status.connected_clients == 0
                assert status.frames_received >= 4
                assert status.frame_age_seconds is not None and status.frame_age_seconds < 2.0
                assert status.live is True
                # ~20 fps configured; measured within a generous band.
                assert status.measured_fps is not None and 5.0 < status.measured_fps < 60.0
            finally:
                proxy.stop()
            assert server.connections == 1

    def test_stopping_the_relay_drops_the_printer_connection(self):
        from kiln.streaming import MJPEGProxy

        with FakeBambuCamera(access_code=ACCESS_CODE, interval=0.02) as server:
            source = _adapter(server).camera_frame_source(port=server.port)
            proxy = MJPEGProxy()
            proxy.start(frame_source=source, port=_free_port(), printer_name="a1")
            deadline = time.monotonic() + 5
            while server.frames_sent < 3 and time.monotonic() < deadline:
                time.sleep(0.02)
            proxy.stop()
            sent = server.frames_sent
            time.sleep(0.3)
            assert server.frames_sent - sent <= 1

    def test_a_refused_camera_is_reported_on_status_not_retried_silently(self):
        from kiln.streaming import MJPEGProxy

        with FakeBambuCamera(access_code="00000000") as server:
            source = _adapter(server, access_code=ACCESS_CODE).camera_frame_source(port=server.port)
            proxy = MJPEGProxy()
            proxy.start(frame_source=source, port=_free_port(), printer_name="a1")
            try:
                deadline = time.monotonic() + 5
                while proxy.status().last_error is None and time.monotonic() < deadline:
                    time.sleep(0.05)
                status = proxy.status()
                assert status.live is False
                assert status.last_error and "access code" in status.last_error
            finally:
                proxy.stop()


# ---------------------------------------------------------------------------
# The capability contract: a backend says whether it can stream and why not
# ---------------------------------------------------------------------------


class TestStreamCapability:
    def test_a_port_6000_family_streams_through_the_relay(self):
        with FakeBambuCamera(access_code=ACCESS_CODE) as server:
            cap = _adapter(server, printer_model="bambu_a1").stream_capability()
        assert cap.available is True
        assert cap.channel == "bambu_port6000"
        assert cap.reason is None
        assert any("lan" in r.lower() for r in cap.requires)
        # Measured 2026-09-15 on an A1: the screen's video toggle does not
        # gate the feed, so it must not be listed as a requirement.
        assert not any("toggle" in r.lower() or "setting" in r.lower() for r in cap.requires)

    def test_an_rtsps_family_says_the_relay_cannot_carry_it(self):
        with FakeBambuCamera(access_code=ACCESS_CODE) as server:
            cap = _adapter(server, printer_model="bambu_x1c").stream_capability()
        assert cap.available is False
        assert cap.channel == "rtsp"
        assert "ffmpeg" in (cap.reason or "").lower()
        assert "snapshot" in (cap.reason or "").lower()

    def test_an_undeclared_model_is_tried_on_port_6000_first(self):
        with FakeBambuCamera(access_code=ACCESS_CODE) as server:
            cap = _adapter(server, printer_model=None).stream_capability()
        assert cap.available is True
        assert cap.channel == "bambu_port6000"

    def test_a_user_supplied_camera_wins(self):
        with FakeBambuCamera(access_code=ACCESS_CODE) as server:
            adapter = _adapter(server)
            adapter.set_external_camera(stream_url="http://cam.local/stream")
            cap = adapter.stream_capability()
            source = adapter.frame_source()
        assert cap.channel == "http_mjpeg" and cap.available is True
        assert source is not None and source.kind == "http_mjpeg"
        assert "cam.local" in source.label

    def test_the_dict_form_is_what_the_tool_reply_carries(self):
        with FakeBambuCamera(access_code=ACCESS_CODE) as server:
            d = _adapter(server, printer_model="bambu_x1c").stream_capability().to_dict()
        assert set(d) >= {"available", "channel", "reason", "requires"}


# ---------------------------------------------------------------------------
# The door: webcam_stream learns the new source, stays a still elsewhere
# ---------------------------------------------------------------------------


@pytest.fixture
def _server_door(monkeypatch):
    from kiln import server as srv

    monkeypatch.setattr(srv, "_stream_proxy", None)
    yield srv
    proxy = getattr(srv, "_stream_proxy", None)
    if proxy is not None and proxy.active:
        proxy.stop()
    monkeypatch.setattr(srv, "_stream_proxy", None)


class TestTheWebcamStreamDoor:
    def test_hosted_says_local_only_instead_of_starting_a_relay(self, _server_door, monkeypatch):
        monkeypatch.setenv("KILN_HOSTED_MULTITENANT", "1")
        result = _server_door.webcam_stream(action="start")
        assert result["success"] is False
        assert result["error"]["code"] == "LOCAL_ONLY"
        assert "computer" in result["error"]["message"].lower()
        assert _server_door._stream_proxy is None or not _server_door._stream_proxy.active

    def test_start_on_a_bambu_relays_port_6000(self, _server_door, monkeypatch):
        monkeypatch.delenv("KILN_HOSTED_MULTITENANT", raising=False)
        with FakeBambuCamera(access_code=ACCESS_CODE, interval=0.02) as server:
            adapter = _adapter(server)
            monkeypatch.setattr(bambu_mod, "_CAMERA_PORT", server.port)
            monkeypatch.setattr(_server_door, "_get_adapter", lambda: adapter)
            monkeypatch.setattr(_server_door, "_get_registry", lambda: mock.Mock(get=lambda n: adapter))
            port = _free_port()
            result = _server_door.webcam_stream(action="start", port=port)
            assert result["success"] is True, result
            assert result["stream"]["source_kind"] == "bambu_port6000"
            assert result["stream"]["local_url"] == f"http://localhost:{port}/stream"
            assert result["capability"]["channel"] == "bambu_port6000"
            assert ACCESS_CODE not in str(result)
            parts = _read_parts(port, 2)
            assert len(parts) == 2
            status = _server_door.webcam_stream(action="status")
            assert status["stream"]["live"] is True
            assert status["stream"]["frame_age_seconds"] is not None
            stopped = _server_door.webcam_stream(action="stop")
            assert stopped["stream"]["active"] is False
            assert server.connections == 1

    def test_an_rtsps_family_is_refused_with_the_reason(self, _server_door, monkeypatch):
        monkeypatch.delenv("KILN_HOSTED_MULTITENANT", raising=False)
        with FakeBambuCamera(access_code=ACCESS_CODE) as server:
            adapter = _adapter(server, printer_model="bambu_x1c")
            monkeypatch.setattr(_server_door, "_get_adapter", lambda: adapter)
            result = _server_door.webcam_stream(action="start", port=_free_port())
        assert result["success"] is False
        assert result["error"]["code"] == "NO_STREAM"
        assert "ffmpeg" in result["error"]["message"].lower()
        assert result["capability"]["channel"] == "rtsp"

    def test_the_still_path_is_untouched(self, _server_door, monkeypatch):
        frames = [make_jpeg(i) for i in range(2)]
        with FakeBambuCamera(access_code=ACCESS_CODE, frames=frames) as server:
            adapter = _adapter(server)
            monkeypatch.setattr(bambu_mod, "_CAMERA_PORT", server.port)
            monkeypatch.setattr(_server_door, "_get_adapter", lambda: adapter)
            result = _server_door.printer_snapshot()
        import base64

        assert result["success"] is True
        assert base64.b64decode(result["image_base64"]) == frames[0]
        assert result["camera_source"] == "printer"
