"""Whether live video worked, per printer model, as classes the heartbeat carries.

A handful of owners run each camera Kiln has never been able to test on
hardware.  Their machines already perform the experiment every time the
print monitor asks for video; until this file, the relay measured the answer
(which feed it opened, whether frames arrived, how fast, why the printer
refused) and threw it away.  Pinned here: the relay and the one planning
helper every door calls record that answer once per session, as closed
tokens; the key's shape is the privacy boundary, so a host, a path or a URL
cannot be spelled in it; and the refusal message offers the camera check on
printers that have one.
"""

from __future__ import annotations

import http.server
import socket
import threading
import time
from unittest import mock

import pytest

from kiln import daily_stats, streaming
from kiln.printers import bambu as bambu_mod
from kiln.printers.bambu import BambuAdapter
from tests._fake_bambu_camera import FakeBambuCamera, make_jpeg

ACCESS_CODE = "12345678"


def _outcomes() -> dict[str, int]:
    return dict(daily_stats.get_daily_stats().get("video_outcomes", {}))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(autouse=True)
def _fresh_process_day(monkeypatch):
    """Plan-level refusals are de-duplicated per process per day; each test
    starts a fresh process-day."""
    monkeypatch.setattr(streaming, "_PLAN_RECORDED", set(), raising=False)
    monkeypatch.setattr(streaming, "_RECONNECT_BACKOFF_SECONDS", 0.05)


def _bambu(server: FakeBambuCamera, **kwargs) -> BambuAdapter:
    defaults = {
        "host": "127.0.0.1", "access_code": server.access_code,
        "serial": "039ABC123", "timeout": 2, "printer_model": "bambu_a1",
    }
    defaults.update(kwargs)
    return BambuAdapter(**defaults)


def _wait_for(predicate, seconds: float = 8.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


class _MjpegHandler(http.server.BaseHTTPRequestHandler):
    frames = [make_jpeg(i, size=3000) for i in range(3)]

    def do_GET(self):  # noqa: N802 — stdlib hook name
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=f")
        self.end_headers()
        try:
            for _ in range(200):
                for jpeg in self.frames:
                    self.wfile.write(b"--f\r\nContent-Type: image/jpeg\r\n")
                    self.wfile.write(f"Content-Length: {len(jpeg)}\r\n\r\n".encode())
                    self.wfile.write(jpeg + b"\r\n")
                    self.wfile.flush()
                    time.sleep(0.02)
        except OSError:
            return

    def log_message(self, *args):
        pass


@pytest.fixture
def mjpeg_server():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _MjpegHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


# ---------------------------------------------------------------------------
# The key is four closed tokens, and that shape is the privacy boundary
# ---------------------------------------------------------------------------


class TestTheKeyShapeIsThePrivacyBoundary:
    def test_a_model_becomes_a_token(self):
        assert daily_stats.video_model_token("Saturn 4 Ultra 16K") == "saturn_4_ultra_16k"
        assert daily_stats.video_model_token("bambu_a1") == "bambu_a1"
        assert daily_stats.video_model_token(None) == "unknown"
        assert daily_stats.video_model_token("   ") == "unknown"

    def test_a_key_that_could_spell_an_address_is_dropped(self):
        daily_stats.record_video_outcome(
            "k1", "http_mjpeg", "user_same_host", "http://192.168.1.5:8080/?action=stream"
        )
        daily_stats.record_video_outcome("k1", "http://x", "printer", "live")
        daily_stats.record_camera_check("k1", "creality mjpeg", "mjpeg")
        assert _outcomes() == {}

    def test_a_well_formed_outcome_is_counted(self):
        daily_stats.record_video_outcome("bambu_a1", "bambu_port6000", "printer", "live")
        daily_stats.record_video_outcome("bambu_a1", "bambu_port6000", "printer", "live")
        assert _outcomes() == {"bambu_a1|bambu_port6000|printer|live": 2}

    def test_a_camera_check_is_counted_under_its_probe(self):
        daily_stats.record_camera_check("creality_k1", "creality_mjpeg_8080", "mjpeg")
        assert _outcomes() == {"creality_k1|check|creality_mjpeg_8080|mjpeg": 1}

    def test_the_vocabulary_has_one_home(self):
        assert "live" in streaming.VIDEO_EVENTS
        assert "user_same_host" in streaming.VIDEO_SOURCES
        assert "http_mjpeg" in streaming.VIDEO_CHANNELS
        assert "webrtc_signalling" in streaming.CHECK_RESULTS
        assert "action_stream" in streaming.ADDRESS_PATH_CLASSES


class TestTheModelIsTheOneTheRelayWasAimedAt:
    def test_the_config_declared_model_is_used(self, monkeypatch):
        import kiln.printer_model_resolver as resolver

        monkeypatch.setattr(resolver, "resolve_printer_model_for", lambda name: "Creality K1")
        assert streaming.video_model_for("k1") == "creality_k1"

    def test_an_unresolvable_model_is_unknown_never_a_raise(self, monkeypatch):
        import kiln.printer_model_resolver as resolver

        def boom(name):
            raise RuntimeError("config unreadable")

        monkeypatch.setattr(resolver, "resolve_printer_model_for", boom)
        assert streaming.video_model_for("k1") == "unknown"

    @pytest.mark.parametrize(
        ("fps", "bucket"),
        [(0.45, "fps_lt1"), (1.0, "fps_1to5"), (4.9, "fps_1to5"), (5.0, "fps_5to15"),
         (14.9, "fps_5to15"), (15.0, "fps_15up"), (60.0, "fps_15up")],
    )
    def test_the_frame_rate_bucket(self, fps, bucket):
        assert streaming._fps_bucket(fps) == bucket


# ---------------------------------------------------------------------------
# The relay records what happened, once per session
# ---------------------------------------------------------------------------


class TestTheRelayRecordsWhatHappened:
    def test_a_live_session_records_start_live_and_one_fps_bucket(self, monkeypatch):
        monkeypatch.setattr(streaming, "video_model_for", lambda name: "bambu_a1")
        with FakeBambuCamera(access_code=ACCESS_CODE, interval=0.02) as server:
            monkeypatch.setattr(bambu_mod, "_CAMERA_PORT", server.port)
            plan = streaming.plan_relay(_bambu(server), printer_name="a1")
            proxy = streaming.MJPEGProxy()
            proxy.start(frame_source=plan.source, port=_free_port(), printer_name="a1")
            try:
                assert _wait_for(
                    lambda: any(k.split("|")[3].startswith("fps_") for k in _outcomes())
                ), _outcomes()
                time.sleep(0.3)  # more frames must not add a second bucket
            finally:
                proxy.stop()
        o = _outcomes()
        assert o["bambu_a1|bambu_port6000|printer|start"] == 1
        assert o["bambu_a1|bambu_port6000|printer|live"] == 1
        fps = [k for k in o if k.split("|")[3].startswith("fps_")]
        assert len(fps) == 1 and o[fps[0]] == 1
        assert fps[0].split("|")[3] in streaming.VIDEO_EVENTS

    def test_a_refused_camera_records_one_refusal_per_session(self, monkeypatch):
        monkeypatch.setattr(streaming, "video_model_for", lambda name: "bambu_a1")
        with FakeBambuCamera(access_code="00000000") as server:
            monkeypatch.setattr(bambu_mod, "_CAMERA_PORT", server.port)
            plan = streaming.plan_relay(_bambu(server, access_code=ACCESS_CODE), printer_name="a1")
            proxy = streaming.MJPEGProxy()
            proxy.start(frame_source=plan.source, port=_free_port(), printer_name="a1")
            try:
                assert _wait_for(lambda: server.rejected >= 3), server.rejected
            finally:
                proxy.stop()
        o = _outcomes()
        assert o.get("bambu_a1|bambu_port6000|printer|refused_access") == 1, o
        assert "bambu_a1|bambu_port6000|printer|live" not in o

    def test_a_plan_refusal_is_recorded_once_per_process_day(self, monkeypatch):
        monkeypatch.setattr(streaming, "video_model_for", lambda name: "bambu_x1c")
        with FakeBambuCamera(access_code=ACCESS_CODE) as server:
            adapter = _bambu(server, printer_model="bambu_x1c")
            streaming.plan_relay(adapter, printer_name="x1c")
            streaming.plan_relay(adapter, printer_name="x1c")
        assert _outcomes() == {"bambu_x1c|rtsp|printer|refused_rtsp": 1}

    def test_a_backend_with_no_stream_records_no_stream(self, monkeypatch):
        monkeypatch.setattr(streaming, "video_model_for", lambda name: "unknown")
        adapter = mock.Mock(spec=["get_stream_url"])
        adapter.get_stream_url.return_value = None
        plan = streaming.plan_relay(adapter)
        assert plan.source is None
        assert _outcomes() == {"unknown|none|printer|refused_no_stream": 1}

    def test_a_user_camera_on_the_printers_own_host_records_port_and_path_class(
        self, monkeypatch, mjpeg_server
    ):
        monkeypatch.setattr(streaming, "video_model_for", lambda name: "creality_k1")
        with FakeBambuCamera(access_code=ACCESS_CODE) as server:
            adapter = _bambu(server)
        adapter.set_external_camera(stream_url=f"http://127.0.0.1:{mjpeg_server}/?action=stream")
        plan = streaming.plan_relay(adapter, printer_name="k1")
        proxy = streaming.MJPEGProxy()
        proxy.start(frame_source=plan.source, port=_free_port(), printer_name="k1")
        try:
            assert _wait_for(lambda: any("|addr_" in k for k in _outcomes())), _outcomes()
        finally:
            proxy.stop()
        o = _outcomes()
        assert o["creality_k1|http_mjpeg|user_same_host|live"] == 1
        assert o[f"creality_k1|http_mjpeg|user_same_host|addr_{mjpeg_server}_action_stream"] == 1

    def test_a_user_camera_elsewhere_records_no_address(self, monkeypatch, mjpeg_server):
        monkeypatch.setattr(streaming, "video_model_for", lambda name: "creality_k1")
        with FakeBambuCamera(access_code=ACCESS_CODE) as server:
            adapter = _bambu(server)
        adapter.set_external_camera(stream_url=f"http://localhost:{mjpeg_server}/stream")
        plan = streaming.plan_relay(adapter, printer_name="k1")
        proxy = streaming.MJPEGProxy()
        proxy.start(frame_source=plan.source, port=_free_port(), printer_name="k1")
        try:
            assert _wait_for(
                lambda: "creality_k1|http_mjpeg|user_other|live" in _outcomes()
            ), _outcomes()
        finally:
            proxy.stop()
        assert not any("|addr_" in k for k in _outcomes())

    def test_every_recorded_key_is_vocabulary_never_a_host_or_a_path(
        self, monkeypatch, mjpeg_server
    ):
        """The privacy property, stated as what IS allowed: every slot after
        the model is a vocabulary token, and an address event is a port and
        a path class — so nothing else can have been written."""
        import re

        monkeypatch.setattr(streaming, "video_model_for", lambda name: "creality_k1")
        with FakeBambuCamera(access_code=ACCESS_CODE) as server:
            adapter = _bambu(server)
        adapter.set_external_camera(
            stream_url=f"http://127.0.0.1:{mjpeg_server}/webcam/?action=stream&token=secret"
        )
        plan = streaming.plan_relay(adapter, printer_name="k1")
        proxy = streaming.MJPEGProxy()
        proxy.start(frame_source=plan.source, port=_free_port(), printer_name="k1")
        try:
            assert _wait_for(lambda: any("|addr_" in k for k in _outcomes())), _outcomes()
        finally:
            proxy.stop()
        address = re.compile(r"^addr_(\d{1,5})_([a-z_]+)$")
        keys = _outcomes()
        assert keys
        for key in keys:
            model, channel, source, event = key.split("|")
            assert model == "creality_k1"
            assert channel in streaming.VIDEO_CHANNELS, key
            assert source in streaming.VIDEO_SOURCES, key
            match = address.match(event)
            if match:
                assert match.group(2) in streaming.ADDRESS_PATH_CLASSES, key
            else:
                assert event in streaming.VIDEO_EVENTS, key
            assert "secret" not in key and "127.0.0.1" not in key
        assert f"creality_k1|http_mjpeg|user_same_host|addr_{mjpeg_server}_webcam_action_stream" in keys


# ---------------------------------------------------------------------------
# A refusal offers the camera check where the printer type has one
# ---------------------------------------------------------------------------


class _NoStream:
    camera_check_ids: tuple[str, ...] = ()

    def get_stream_url(self):
        return None


class _NoStreamWithChecks(_NoStream):
    camera_check_ids = ("example_probe",)


class TestTheRefusalOffersTheCheck:
    def test_an_adapter_with_camera_checks_offers_it(self, monkeypatch):
        monkeypatch.setattr(streaming, "video_model_for", lambda name: "unknown")
        plan = streaming.plan_relay(_NoStreamWithChecks())
        assert plan.source is None
        assert plan.check_available is True
        assert plan.message.endswith("Ask Kiln to check this printer's camera to see what it serves.")

    def test_an_adapter_without_checks_does_not(self, monkeypatch):
        monkeypatch.setattr(streaming, "video_model_for", lambda name: "unknown")
        plan = streaming.plan_relay(_NoStream())
        assert plan.check_available is False
        assert "check this printer's camera" not in (plan.message or "")
