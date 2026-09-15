"""The camera check: what a printer's likely camera addresses actually serve.

A user who is told "no live video for this printer" can ask Kiln to look.
The check tries the addresses the printer type is known to use, on the
printer's own host, with one bounded GET each, and says what answered: a
live MJPEG stream the relay can carry, a still, a WebRTC page, a web page,
an error, or nothing.  Pinned here: it only reads (never an offer, never a
redirect followed, never a connection left open), each read is bounded in
bytes and time, the basis says whose documentation an address came from,
the check runs only when asked and registers nothing, and the telemetry key
names the probe and the result, never the address.

Every server here listens on 127.0.0.1; nothing reaches the real network.
"""

from __future__ import annotations

import http.server
import importlib
import inspect
import json
import re
import socket
import threading
import time
from unittest import mock

import pytest
from click.testing import CliRunner

from kiln import daily_stats, streaming

_JPEG = b"\xff\xd8\xff\xe0" + b"0" * 64 + b"\xff\xd9"
_PROBE_ID = re.compile(r"^[a-z0-9_]{3,32}$")
_RESULT_KEYS = {"probe_id", "result", "detail", "http_status", "content_type", "url", "basis"}


def _cc():
    """The module under test, imported per test so each test fails on its own."""
    return importlib.import_module("kiln.camera_check")


def _outcomes() -> dict[str, int]:
    return dict(daily_stats.get_daily_stats().get("video_outcomes", {}))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(autouse=True)
def _fresh_process_day(monkeypatch):
    monkeypatch.setattr(streaming, "_PLAN_RECORDED", set(), raising=False)
    monkeypatch.delenv("KILN_HOSTED_MULTITENANT", raising=False)


# ---------------------------------------------------------------------------
# Local servers
# ---------------------------------------------------------------------------


class _Server:
    """A loopback HTTP server that answers GET with *respond* and records
    every request method, so a test can see exactly what the check sent."""

    def __init__(self, respond) -> None:
        self.methods: list[str] = []
        self.body_lengths: list[int] = []
        self.client_gone = threading.Event()
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 — stdlib hook name
                outer.methods.append("GET")
                outer.body_lengths.append(int(self.headers.get("Content-Length") or 0))
                try:
                    respond(self)
                except OSError:
                    outer.client_gone.set()

            def do_POST(self):  # noqa: N802 — stdlib hook name
                outer.methods.append("POST")
                self.send_error(405)

            def log_message(self, *args):
                pass

        self._httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"

    def __enter__(self) -> _Server:
        threading.Thread(
            target=self._httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        ).start()
        return self

    def __exit__(self, *exc) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


def _mjpeg(handler) -> None:
    handler.send_response(200)
    handler.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
    handler.end_headers()
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        handler.wfile.write(
            b"--frame\r\nContent-Type: image/jpeg\r\n"
            + f"Content-Length: {len(_JPEG)}\r\n\r\n".encode()
            + _JPEG
            + b"\r\n"
        )
        handler.wfile.flush()
        time.sleep(0.02)


def _jpeg(handler) -> None:
    handler.send_response(200)
    handler.send_header("Content-Type", "image/jpeg")
    handler.send_header("Content-Length", str(len(_JPEG)))
    handler.end_headers()
    handler.wfile.write(_JPEG)


def _page(body: bytes, *, status: int = 200, content_type: str = "text/html; charset=utf-8"):
    def respond(handler) -> None:
        handler.send_response(status)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    return respond


def _missing(handler) -> None:
    handler.send_error(404)  # the stdlib error page is text/html


def _trickle(handler) -> None:
    handler.send_response(200)
    handler.send_header("Content-Type", "text/html")
    handler.end_headers()
    handler.wfile.write(b"<html>")
    handler.wfile.flush()
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        handler.wfile.write(b"a")
        handler.wfile.flush()
        time.sleep(0.2)


def _assert_vocabulary(result: str) -> None:
    assert result in streaming.CHECK_RESULTS, result


class _FakePrinter:
    """Only what the check reads: declared ids, probes, and no user camera."""

    external_camera = None

    def __init__(self, probes) -> None:
        self._probes = list(probes)
        self.camera_check_ids = tuple(p.probe_id for p in self._probes)
        self.registered: list[dict] = []

    def camera_probes(self):
        return list(self._probes)

    def set_external_camera(self, **kwargs) -> None:
        self.registered.append(kwargs)


class _NoStream:
    camera_check_ids: tuple[str, ...] = ()
    external_camera = None

    def get_stream_url(self):
        return None

    def camera_probes(self):
        raise AssertionError("a printer type without a check must not be probed")


class _NoStreamWithChecks(_NoStream):
    camera_check_ids = ("example_probe",)


# ---------------------------------------------------------------------------
# The classifier: one bounded GET, always closed
# ---------------------------------------------------------------------------


class TestClassifyCameraAddress:
    def test_a_multipart_stream_is_mjpeg_and_the_connection_is_closed(self):
        with _Server(_mjpeg) as srv:
            started = time.monotonic()
            result, detail, status, content_type = _cc().classify_camera_address(
                f"{srv.url}/?action=stream"
            )
            elapsed = time.monotonic() - started
            assert (result, status, content_type) == ("mjpeg", 200, "multipart/x-mixed-replace")
            assert detail and elapsed < 2.0
            assert srv.client_gone.wait(3.0), "the check left the stream open"
        _assert_vocabulary(result)

    def test_a_single_image_is_jpeg(self):
        with _Server(_jpeg) as srv:
            result, _, status, content_type = _cc().classify_camera_address(f"{srv.url}/snapshot")
        assert (result, status, content_type) == ("jpeg", 200, "image/jpeg")

    @pytest.mark.parametrize("marker", [b"RTCPeerConnection", b"rtcpeerconnection"])
    def test_a_page_that_opens_a_webrtc_peer_connection_is_webrtc_signalling(self, marker):
        body = b"<html><script>const pc = new " + marker + b"();</script></html>"
        with _Server(_page(body)) as srv:
            result, _, status, _ = _cc().classify_camera_address(f"{srv.url}/")
        assert (result, status) == ("webrtc_signalling", 200)

    def test_a_plain_page_is_html(self):
        with _Server(_page(b"<html><body>printer</body></html>")) as srv:
            result, _, status, content_type = _cc().classify_camera_address(f"{srv.url}/")
        assert (result, status, content_type) == ("html", 200, "text/html")

    def test_an_error_status_is_http_error_even_with_an_html_body(self):
        with _Server(_missing) as srv:
            result, detail, status, _ = _cc().classify_camera_address(f"{srv.url}/webcam/")
        assert (result, status) == ("http_error", 404)
        assert "404" in detail

    def test_a_closed_port_is_unreachable(self):
        result, detail, status, content_type = _cc().classify_camera_address(
            f"http://127.0.0.1:{_free_port()}/?action=stream"
        )
        assert (result, status, content_type) == ("unreachable", None, None)
        assert detail

    def test_a_server_that_never_answers_is_unreachable_within_the_timeout(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(4)  # the kernel completes the handshake; nobody answers
        try:
            started = time.monotonic()
            result, _, status, _ = _cc().classify_camera_address(
                f"http://127.0.0.1:{listener.getsockname()[1]}/", timeout=0.5
            )
            elapsed = time.monotonic() - started
        finally:
            listener.close()
        assert (result, status) == ("unreachable", None)
        assert elapsed < 2.0

    @pytest.mark.parametrize("without_read1", [False, True], ids=["read1", "without_read1"])
    def test_a_trickling_body_is_bounded_in_time(self, monkeypatch, without_read1):
        if without_read1:  # an older urllib3 has no read1; the fallback must hold the same bound
            import urllib3.response

            # Removed wherever the response's classes define it: deleting only
            # the concrete one exposes a base-class read1 that is not implemented.
            for cls in urllib3.response.HTTPResponse.__mro__:
                if "read1" in vars(cls):
                    monkeypatch.delattr(cls, "read1")
        with _Server(_trickle) as srv:
            started = time.monotonic()
            result, _, status, _ = _cc().classify_camera_address(f"{srv.url}/", timeout=1.0)
            elapsed = time.monotonic() - started
        assert (result, status) == ("html", 200)
        assert elapsed < 3.5, f"a trickling server held the check for {elapsed:.1f}s"

    def test_the_body_read_stops_at_max_bytes(self):
        body = b"<html>" + b"a" * 70_000 + b"new RTCPeerConnection()</html>"
        with _Server(_page(body)) as srv:
            bounded = _cc().classify_camera_address(f"{srv.url}/")
            wider = _cc().classify_camera_address(f"{srv.url}/", max_bytes=200_000)
        assert bounded[0] == "html"
        assert wider[0] == "webrtc_signalling"

    def test_a_redirect_is_not_followed(self):
        with _Server(_page(b"elsewhere")) as target:

            def redirect(handler) -> None:
                handler.send_response(302)
                handler.send_header("Location", f"{target.url}/")
                handler.send_header("Content-Length", "0")
                handler.end_headers()

            with _Server(redirect) as srv:
                result, detail, status, _ = _cc().classify_camera_address(f"{srv.url}/")
            assert target.methods == []
        assert (result, status) == ("other", 302)
        assert "302" in detail

    def test_an_address_that_is_not_http_is_not_opened(self):
        result, detail, status, content_type = _cc().classify_camera_address(
            "rtsp://127.0.0.1:554/video"
        )
        assert (result, status, content_type) == ("other", None, None)
        assert "rtsp" in detail

    def test_only_a_get_with_no_body_is_ever_sent(self):
        with _Server(_page(b"<html>signalling</html>")) as srv:
            _cc().classify_camera_address(f"{srv.url}/call/webrtc_local")
        assert srv.methods == ["GET"]
        assert srv.body_lengths == [0]


# ---------------------------------------------------------------------------
# What each printer type offers to check
# ---------------------------------------------------------------------------


def _creality(host: str):
    from kiln.printers.creality import CrealityAdapter

    ok = mock.MagicMock(ok=True, status_code=200)
    ok.json.return_value = {"result": {"klippy_state": "ready"}}
    with mock.patch("kiln.printers.creality.requests.get", return_value=ok):
        return CrealityAdapter(host, timeout=5, retries=1)


def _elegoo():
    from kiln.printers.elegoo import ElegooAdapter

    return ElegooAdapter(host="192.168.1.50", mainboard_id="ABCD1234ABCD1234", timeout=2)


def _video_reply(**data):
    return {"Cmd": 386, "Data": data, "RequestID": "r1"}


class TestWhatEachPrinterTypeOffers:
    def test_the_base_adapter_offers_no_check(self):
        from kiln.printers.base import PrinterAdapter

        assert PrinterAdapter.camera_check_ids == ()
        assert PrinterAdapter.camera_probes(mock.Mock()) == []

    def test_a_probe_id_must_be_a_token(self):
        with pytest.raises(ValueError):
            _cc().CameraProbe("creality mjpeg", "http://192.168.1.5:8080/", "basis")

    def test_every_exported_adapter_declares_its_ids_as_tokens(self):
        import kiln.printers as printers
        from kiln.printers.base import PrinterAdapter

        adapters = [
            obj
            for name in printers.__all__
            if inspect.isclass(obj := getattr(printers, name, None)) and issubclass(obj, PrinterAdapter)
        ]
        assert adapters
        for cls in adapters:
            ids = cls.camera_check_ids
            assert isinstance(ids, tuple), cls.__name__
            assert len(set(ids)) == len(ids), cls.__name__
            assert all(_PROBE_ID.match(i) for i in ids), (cls.__name__, ids)

    @pytest.mark.parametrize(
        ("registered", "host"),
        [
            ("k1-max.local", "k1-max.local"),
            ("http://192.168.1.55", "192.168.1.55"),
            ("http://192.168.1.55:7125", "192.168.1.55"),
            ("http://[fe80::1]:7125", "[fe80::1]"),
        ],
    )
    def test_creality_probes_the_printers_own_host_without_its_port(self, registered, host):
        adapter = _creality(registered)
        probes = adapter.camera_probes()
        assert type(adapter).camera_check_ids == (
            "creality_fluidd_webcam", "creality_mjpeg_8080", "creality_webrtc_8000",
        )
        assert [p.probe_id for p in probes] == list(type(adapter).camera_check_ids)
        assert [p.url for p in probes] == [
            f"http://{host}:4408/webcam/?action=stream",
            f"http://{host}:8080/?action=stream",
            f"http://{host}:8000/call/webrtc_local",
        ]

    def test_creality_probes_are_built_without_touching_the_network(self):
        adapter = _creality("k1-max.local")
        with (
            mock.patch("requests.Session.request", side_effect=AssertionError("network")),
            mock.patch("kiln.printers.creality.requests.get", side_effect=AssertionError("network")),
        ):
            assert len(adapter.camera_probes()) == 3

    def test_creality_basis_says_which_addresses_come_from_a_community_source(self):
        probes = {p.probe_id: p for p in _creality("k1-max.local").camera_probes()}
        maker = probes["creality_fluidd_webcam"].basis.lower()
        assert "creality" in maker and "community" not in maker
        for probe_id in ("creality_mjpeg_8080", "creality_webrtc_8000"):
            assert "community" in probes[probe_id].basis.lower(), probe_id

    def test_elegoo_probes_the_address_the_printer_gave(self):
        from kiln.printers.elegoo import ElegooAdapter

        adapter = _elegoo()
        reply = _video_reply(Ack=0, VideoUrl="192.168.1.50:3031/video")
        with mock.patch.object(adapter, "_send_command", return_value=reply) as send:
            probes = adapter.camera_probes()
        assert ElegooAdapter.camera_check_ids == ("elegoo_video_url",)
        assert [(p.probe_id, p.url) for p in probes] == [
            ("elegoo_video_url", "http://192.168.1.50:3031/video")
        ]
        assert send.call_args.args[0] == 386
        assert "386" in probes[0].basis and "VideoUrl" in probes[0].basis

    @pytest.mark.parametrize(
        "reply",
        [None, _video_reply(Ack=0), _video_reply(Ack=0, VideoUrl=""), _video_reply(Ack=1)],
    )
    def test_elegoo_without_an_address_offers_nothing(self, reply):
        adapter = _elegoo()
        with mock.patch.object(adapter, "_send_command", return_value=reply):
            assert adapter.camera_probes() == []

    def test_elegoo_asks_the_printer_even_when_a_user_camera_is_registered(self):
        adapter = _elegoo()
        adapter.set_external_camera(stream_url="http://cam.local/stream")
        reply = _video_reply(Ack=0, VideoUrl="192.168.1.50:3031/video")
        with mock.patch.object(adapter, "_send_command", return_value=reply) as send:
            probes = adapter.camera_probes()
        assert [p.url for p in probes] == ["http://192.168.1.50:3031/video"]
        send.assert_called_once()

    def test_elegoo_detail_says_the_check_competes_for_a_video_connection(self, monkeypatch):
        monkeypatch.setattr(streaming, "video_model_for", lambda name: "centauri_carbon")
        adapter = _elegoo()
        with _Server(_mjpeg) as srv:
            reply = _video_reply(Ack=0, VideoUrl=f"127.0.0.1:{srv.port}/video")
            with mock.patch.object(adapter, "_send_command", return_value=reply):
                results = _cc().run_camera_checks(adapter)
        assert [r.result for r in results] == ["mjpeg"]
        assert "simultaneous" in results[0].detail
        assert "MaximumVideoStreamAllowed" in results[0].detail


# ---------------------------------------------------------------------------
# Telemetry: one key per check, never an address
# ---------------------------------------------------------------------------


class TestWhatTheCheckRecords:
    def test_each_check_records_exactly_one_key(self, monkeypatch):
        cc = _cc()
        names: list[str | None] = []
        monkeypatch.setattr(streaming, "video_model_for", lambda name: names.append(name) or "k1_max")
        with _Server(_mjpeg) as live:
            adapter = _FakePrinter([
                cc.CameraProbe("example_live", f"{live.url}/?action=stream", "a loopback test server"),
                cc.CameraProbe("example_closed", f"http://127.0.0.1:{_free_port()}/", "a closed port"),
            ])
            results = cc.run_camera_checks(adapter, printer_name="k1")
        assert [r.result for r in results] == ["mjpeg", "unreachable"]
        assert _outcomes() == {
            "k1_max|check|example_live|mjpeg": 1,
            "k1_max|check|example_closed|unreachable": 1,
        }
        assert set(names) == {"k1"}

    def test_the_recorder_never_receives_an_address(self, monkeypatch):
        cc = _cc()
        calls: list[tuple] = []
        monkeypatch.setattr(streaming, "video_model_for", lambda name: "k1_max")
        monkeypatch.setattr(daily_stats, "record_camera_check", lambda *a, **kw: calls.append((a, kw)))
        with _Server(_page(b"<html></html>")) as srv:
            adapter = _FakePrinter([cc.CameraProbe("example_page", f"{srv.url}/webcam/", "a test")])
            cc.run_camera_checks(adapter, printer_name="k1")
        assert calls == [(("k1_max", "example_page", "html"), {})]

    def test_a_printer_without_probes_records_nothing(self):
        assert _cc().run_camera_checks(object()) == []
        assert _outcomes() == {}


# ---------------------------------------------------------------------------
# The door: webcam_stream(action="check")
# ---------------------------------------------------------------------------


@pytest.fixture
def door(monkeypatch):
    from kiln import server as srv

    monkeypatch.setattr(srv, "_video_route_block_for", lambda name: None)
    monkeypatch.setattr(srv, "_stream_proxy", None)
    yield srv
    proxy = getattr(srv, "_stream_proxy", None)
    if proxy is not None and proxy.active:
        proxy.stop()


class TestTheCheckDoor:
    def test_the_reply_carries_each_check_a_summary_and_the_next_step(self, door, monkeypatch):
        cc = _cc()
        monkeypatch.setattr(streaming, "video_model_for", lambda name: "k1_max")
        with _Server(_mjpeg) as live, _Server(_missing) as missing:
            stream_url = f"{live.url}/?action=stream"
            adapter = _FakePrinter([
                cc.CameraProbe("example_live", stream_url, "a loopback test server"),
                cc.CameraProbe("example_missing", f"{missing.url}/webcam/", "a missing page"),
            ])
            monkeypatch.setattr(door, "_get_adapter", lambda: adapter)
            reply = door.webcam_stream(action="check")
        assert reply["success"] is True, reply
        assert set(reply) == {"success", "checks", "summary", "next_step"}
        assert [(c["probe_id"], c["result"]) for c in reply["checks"]] == [
            ("example_live", "mjpeg"), ("example_missing", "http_error"),
        ]
        assert all(set(c) == _RESULT_KEYS for c in reply["checks"])
        assert isinstance(reply["summary"], str) and reply["summary"]
        assert "register_printer" in reply["next_step"]
        assert "camera_stream_url" in reply["next_step"]
        assert "kiln auth" not in reply["next_step"]
        assert stream_url in reply["next_step"]

    def test_no_stream_found_means_no_next_step(self, door, monkeypatch):
        cc = _cc()
        monkeypatch.setattr(streaming, "video_model_for", lambda name: "k1_max")
        with _Server(_missing) as missing:
            adapter = _FakePrinter([cc.CameraProbe("example_missing", f"{missing.url}/", "a test")])
            monkeypatch.setattr(door, "_get_adapter", lambda: adapter)
            reply = door.webcam_stream(action="check")
        assert reply["success"] is True
        assert reply["next_step"] is None

    def test_hosted_refuses_before_any_adapter_is_touched(self, door, monkeypatch):
        monkeypatch.setenv("KILN_HOSTED_MULTITENANT", "1")
        touched: list[str] = []
        monkeypatch.setattr(door, "_get_adapter", lambda: touched.append("adapter"))
        monkeypatch.setattr(door, "_get_registry", lambda: touched.append("registry"))
        for name in (None, "k1"):
            reply = door.webcam_stream(printer_name=name, action="check")
            assert reply["success"] is False
            assert reply["error"]["code"] == "LOCAL_ONLY"
            assert reply["error"]["message"] == streaming.LOCAL_ONLY_MESSAGE
        assert touched == []

    def test_a_printer_type_without_a_check_is_refused_in_words(self, door, monkeypatch):
        monkeypatch.setattr(door, "_get_adapter", lambda: _NoStream())
        reply = door.webcam_stream(action="check")
        assert reply["success"] is False
        assert reply["error"]["code"] == "NO_CAMERA_CHECK"
        assert len(reply["error"]["message"]) > 20

    def test_the_check_never_registers_a_camera(self, door, monkeypatch):
        cc = _cc()
        monkeypatch.setattr(streaming, "video_model_for", lambda name: "k1_max")
        registered: list[tuple] = []
        monkeypatch.setattr(door, "register_printer", lambda *a, **kw: registered.append((a, kw)))
        with _Server(_mjpeg) as live:
            adapter = _FakePrinter([cc.CameraProbe("example_live", f"{live.url}/?action=stream", "a test")])
            registry = mock.Mock()
            registry.get.return_value = adapter
            monkeypatch.setattr(door, "_get_registry", lambda: registry)
            reply = door.webcam_stream(printer_name="k1", action="check")
        assert reply["checks"][0]["result"] == "mjpeg"
        assert [call[0] for call in registry.method_calls] == ["get"]
        assert registered == []
        assert adapter.registered == []

    def test_a_named_printers_model_reaches_the_recorder_from_a_check(self, door, monkeypatch):
        cc = _cc()
        names: list[str | None] = []
        monkeypatch.setattr(streaming, "video_model_for", lambda name: names.append(name) or "creality_k1")
        with _Server(_mjpeg) as live:
            adapter = _FakePrinter([cc.CameraProbe("example_live", f"{live.url}/?action=stream", "a test")])
            registry = mock.Mock()
            registry.get.return_value = adapter
            monkeypatch.setattr(door, "_get_registry", lambda: registry)
            door.webcam_stream(printer_name="k1", action="check")
        assert set(names) == {"k1"}
        assert _outcomes() == {"creality_k1|check|example_live|mjpeg": 1}

    def test_a_named_printers_model_reaches_the_recorder_from_a_start(self, door, monkeypatch):
        names: list[str | None] = []
        monkeypatch.setattr(streaming, "video_model_for", lambda name: names.append(name) or "creality_k1")
        registry = mock.Mock()
        registry.get.return_value = _NoStreamWithChecks()
        monkeypatch.setattr(door, "_get_registry", lambda: registry)
        reply = door.webcam_stream(printer_name="k1", action="start")
        assert reply["success"] is False
        assert names == ["k1"]
        outcomes = _outcomes()
        assert outcomes and all(key.startswith("creality_k1|") for key in outcomes), outcomes

    def test_a_start_refusal_offers_the_check_once_where_one_exists(self, door, monkeypatch):
        monkeypatch.setattr(streaming, "video_model_for", lambda name: "unknown")
        monkeypatch.setattr(door, "_get_adapter", lambda: _NoStreamWithChecks())
        offered = door.webcam_stream(action="start")
        assert offered["check_available"] is True
        assert offered["error"]["message"].count(streaming.CHECK_OFFER) == 1
        monkeypatch.setattr(door, "_get_adapter", lambda: _NoStream())
        plain = door.webcam_stream(action="start")
        assert "check_available" not in plain
        assert streaming.CHECK_OFFER not in plain["error"]["message"]

    def test_an_elegoo_that_gives_no_address_says_why(self, door, monkeypatch):
        adapter = _elegoo()
        monkeypatch.setattr(door, "_get_adapter", lambda: adapter)
        with mock.patch.object(adapter, "_send_command", return_value=_video_reply(Ack=1)):
            reply = door.webcam_stream(action="check")
        assert reply["success"] is True
        assert reply["checks"] == []
        assert "simultaneous" in reply["summary"]
        assert reply["next_step"] is None
        assert _outcomes() == {}


# ---------------------------------------------------------------------------
# The CLI door: kiln stream --check
# ---------------------------------------------------------------------------


def _cli():
    from kiln.cli.main import cli

    return cli


class TestTheCliCheck:
    def test_json_output_carries_the_same_reply(self, monkeypatch):
        cc = _cc()
        names: list[str | None] = []
        monkeypatch.setattr(streaming, "video_model_for", lambda name: names.append(name) or "k1_max")
        with _Server(_mjpeg) as live:
            adapter = _FakePrinter([cc.CameraProbe("example_live", f"{live.url}/?action=stream", "a test")])
            with mock.patch("kiln.cli.main._get_adapter_from_ctx", return_value=adapter):
                result = CliRunner().invoke(_cli(), ["--printer", "k1", "stream", "--check", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["status"] == "success"
        assert [c["result"] for c in data["data"]["checks"]] == ["mjpeg"]
        assert data["data"]["summary"]
        # A person at the terminal saves a camera with kiln auth, not a tool.
        next_step = data["data"]["next_step"]
        assert "kiln auth" in next_step and "--camera-stream-url" in next_step
        assert "register_printer" not in next_step
        assert set(names) == {"k1"}
        assert adapter.registered == []

    def test_human_output_names_each_result(self, monkeypatch):
        cc = _cc()
        monkeypatch.setattr(streaming, "video_model_for", lambda name: "k1_max")
        with _Server(_missing) as missing:
            adapter = _FakePrinter([cc.CameraProbe("example_missing", f"{missing.url}/", "a test")])
            with mock.patch("kiln.cli.main._get_adapter_from_ctx", return_value=adapter):
                result = CliRunner().invoke(_cli(), ["stream", "--check"])
        assert result.exit_code == 0, result.output
        assert "example_missing" in result.output
        assert "http_error" in result.output

    def test_hosted_refuses_before_the_printer_is_resolved(self, monkeypatch):
        monkeypatch.setenv("KILN_HOSTED_MULTITENANT", "1")
        with mock.patch(
            "kiln.cli.main._get_adapter_from_ctx", side_effect=AssertionError("adapter resolved")
        ):
            result = CliRunner().invoke(_cli(), ["stream", "--check", "--json"])
        assert result.exit_code != 0
        assert "LOCAL_ONLY" in result.output

    def test_a_printer_type_without_a_check_exits_nonzero(self):
        with mock.patch("kiln.cli.main._get_adapter_from_ctx", return_value=mock.MagicMock()):
            result = CliRunner().invoke(_cli(), ["stream", "--check", "--json"])
        assert result.exit_code != 0
        assert "NO_CAMERA_CHECK" in result.output

    def test_check_and_stop_together_is_refused_rather_than_one_ignored(self):
        with mock.patch(
            "kiln.cli.main._get_adapter_from_ctx", side_effect=AssertionError("adapter resolved")
        ):
            result = CliRunner().invoke(_cli(), ["stream", "--check", "--stop", "--json"])
        assert result.exit_code != 0
        assert "BAD_REQUEST" in result.output

    def test_a_start_names_the_printer_so_its_model_is_recorded(self, monkeypatch):
        names: list[str | None] = []
        monkeypatch.setattr(streaming, "video_model_for", lambda name: names.append(name) or "creality_k1")
        with mock.patch("kiln.cli.main._get_adapter_from_ctx", return_value=_NoStreamWithChecks()):
            result = CliRunner().invoke(_cli(), ["--printer", "k1", "stream", "--json"])
        assert result.exit_code != 0
        assert names == ["k1"]
        assert result.output.count(streaming.CHECK_OFFER) == 1
