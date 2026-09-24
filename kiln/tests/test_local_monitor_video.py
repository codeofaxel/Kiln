"""The monitor wire's ``video`` axis: live video for the print monitor.

The panel used to get one camera frame every thirty seconds.  With the
relay able to read a printer's own camera feed, the local door can hand the
panel a live stream instead — under the same room-camera rule as the still
(frames only while a print is on the machine), carrying the frame's age so
a frozen picture is never presented as live, and with an honest note where
there is no video to give.
"""

from __future__ import annotations

import inspect

import pytest

from kiln import local_monitor
from kiln.monitor_payload import compose_monitor_payload


@pytest.fixture(autouse=True)
def _reset(monkeypatch, tmp_path):
    monkeypatch.setenv("KILN_HOME", str(tmp_path / "kiln_home"))
    monkeypatch.delenv("KILN_HOSTED_MULTITENANT", raising=False)
    local_monitor._reset_for_tests()
    yield
    local_monitor._reset_for_tests()


_ACTIVE = {"success": True, "printer": {"state": "printing"}, "printer_model": "bambu_a1"}
_IDLE = {"success": True, "printer": {"state": "idle"}, "printer_model": "bambu_a1"}

_BLOCK = {
    "live_url": "http://localhost:8081/stream",
    "source": "bambu_port6000",
    "frame_age_seconds": 0.4,
    "live": True,
    "measured_fps": 2.0,
}


class TestTheWireCarriesVideo:
    def test_the_block_rides_under_video_with_only_its_known_keys(self):
        payload = compose_monitor_payload(
            None, None, _ACTIVE, None, None, None,
            video={**_BLOCK, "secret": "no"},
        )
        assert payload["video"] == _BLOCK
        assert "video_note" not in payload

    def test_the_note_rides_alone_when_there_is_no_video(self):
        payload = compose_monitor_payload(
            None, None, _IDLE, None, None, None,
            video=None, video_note="video is off while no print is active",
        )
        assert "video" not in payload
        assert payload["video_note"] == "video is off while no print is active"

    def test_neither_key_appears_unless_asked(self):
        payload = compose_monitor_payload(None, None, _ACTIVE, None, None, None)
        assert "video" not in payload and "video_note" not in payload


class TestTheLocalDoorAsksTheRelay:
    def test_video_only_rides_when_asked(self, monkeypatch):
        monkeypatch.setattr(local_monitor, "_direct_status", lambda pn, detail="lite": (_ACTIVE, None))
        monkeypatch.setattr(local_monitor, "_coverage_block", lambda pn: None)
        monkeypatch.setattr(local_monitor, "_video_block", lambda pn, status: (_BLOCK, None))
        with_video = local_monitor.compose_local_payload(include_video=True)
        without = local_monitor.compose_local_payload(include_video=False)
        assert with_video["video"] == _BLOCK
        assert "video" not in without and "video_note" not in without

    def test_the_room_camera_rule_holds_for_video_and_stops_a_running_relay(self, monkeypatch):
        stopped: list[str] = []

        class _Proxy:
            active = True
            printer_name = "default"

            def stop(self):
                stopped.append("stopped")

        from kiln import server as srv

        monkeypatch.setattr(srv, "_get_stream_proxy", lambda: _Proxy())
        block, note = local_monitor._video_block(None, _IDLE)
        assert block is None
        assert note == "video is off while no print is active"
        assert stopped == ["stopped"]

    def test_an_active_print_starts_the_relay_and_maps_its_status(self, monkeypatch):
        calls: list[dict] = []

        def fake_webcam_stream(**kwargs):
            calls.append(kwargs)
            return {
                "success": True,
                "stream": {
                    "active": True,
                    "local_url": "http://localhost:8081/stream",
                    "source_kind": "bambu_port6000",
                    "frame_age_seconds": 0.4,
                    "live": True,
                    "measured_fps": 2.0,
                    "last_error": None,
                },
                "capability": {"available": True, "channel": "bambu_port6000"},
            }

        from kiln import server as srv

        monkeypatch.setattr(srv, "webcam_stream", fake_webcam_stream)
        block, note = local_monitor._video_block("a1", _ACTIVE)
        assert note is None
        assert block == _BLOCK
        assert calls == [{"printer_name": "a1", "action": "start", "port": 8081}]

    def test_a_refusal_becomes_the_note_not_an_error(self, monkeypatch):
        from kiln import server as srv

        monkeypatch.setattr(
            srv,
            "webcam_stream",
            lambda **kw: {
                "success": False,
                "error": {"code": "NO_STREAM", "message": "This model's camera streams over RTSPS."},
            },
        )
        block, note = local_monitor._video_block(None, _ACTIVE)
        assert block is None
        assert note == "This model's camera streams over RTSPS."

    def test_a_relay_with_no_frame_yet_is_not_live(self, monkeypatch):
        from kiln import server as srv

        monkeypatch.setattr(
            srv,
            "webcam_stream",
            lambda **kw: {
                "success": True,
                "stream": {
                    "active": True,
                    "local_url": "http://localhost:8081/stream",
                    "source_kind": "bambu_port6000",
                    "frame_age_seconds": None,
                    "live": False,
                    "measured_fps": None,
                    "last_error": "The printer closed the camera connection without sending a frame.",
                },
            },
        )
        block, note = local_monitor._video_block(None, _ACTIVE)
        assert block is not None and block["live"] is False
        assert note == "The printer closed the camera connection without sending a frame."

    def test_hosted_says_local_only(self, monkeypatch):
        monkeypatch.setenv("KILN_HOSTED_MULTITENANT", "1")
        block, note = local_monitor._video_block(None, _ACTIVE)
        assert block is None
        assert "computer" in (note or "").lower()


class TestThePollVerbTakesTheFlag:
    def test_the_snapshot_verb_accepts_include_video(self):
        captured: dict = {}

        class _Mcp:
            _tool_manager = type("T", (), {"_tools": {}})()

            def tool(self, **kw):
                def deco(fn):
                    captured["fn"] = fn
                    return fn

                return deco

        assert local_monitor._register_snapshot_verb(_Mcp())
        params = inspect.signature(captured["fn"]).parameters
        assert "include_video" in params


class TestThePanelMayLoadTheRelay:
    def test_the_resource_declares_the_relay_origin_and_nothing_else(self):
        captured: dict = {}

        class _Mcp:
            def add_resource(self, resource):
                captured["meta"] = resource.meta

        assert local_monitor._register_resource(_Mcp())
        csp = captured["meta"]["ui"]["csp"]
        assert csp == {"resourceDomains": ["http://127.0.0.1:8081", "http://localhost:8081"]}
        assert "connectDomains" not in csp

    def test_the_relay_port_matches_the_declared_origin(self):
        assert f":{local_monitor.VIDEO_RELAY_PORT}" in local_monitor.PANEL_CSP["resourceDomains"][0]
