"""Tests for kiln.streaming — MJPEG proxy server."""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from kiln.streaming import MJPEGProxy, StreamInfo


# ---------------------------------------------------------------------------
# StreamInfo tests
# ---------------------------------------------------------------------------

class TestStreamInfo:
    def test_defaults(self):
        info = StreamInfo(active=False)
        assert info.active is False
        assert info.local_url is None
        assert info.source_url is None
        assert info.printer_name is None
        assert info.connected_clients == 0
        assert info.frames_served == 0
        assert info.uptime_seconds == 0.0

    def test_to_dict(self):
        info = StreamInfo(
            active=True,
            local_url="http://localhost:8081/stream",
            source_url="http://printer/webcam/?action=stream",
            printer_name="voron",
            connected_clients=2,
            frames_served=100,
            uptime_seconds=60.5,
        )
        d = info.to_dict()
        assert d["active"] is True
        assert d["local_url"] == "http://localhost:8081/stream"
        assert d["printer_name"] == "voron"
        assert d["connected_clients"] == 2
        assert d["frames_served"] == 100
        assert d["uptime_seconds"] == 60.5

    def test_to_dict_inactive(self):
        info = StreamInfo(active=False)
        d = info.to_dict()
        assert d["active"] is False
        assert d["local_url"] is None

    def test_source_url_field(self):
        info = StreamInfo(
            active=True,
            source_url="http://printer/webcam/?action=stream",
        )
        assert info.source_url == "http://printer/webcam/?action=stream"


# ---------------------------------------------------------------------------
# MJPEGProxy unit tests (no real server/threads)
# ---------------------------------------------------------------------------

class TestMJPEGProxyUnit:
    def test_initial_state(self):
        proxy = MJPEGProxy()
        assert proxy.active is False

    def test_status_when_not_started(self):
        proxy = MJPEGProxy()
        status = proxy.status()
        assert status.active is False
        assert status.local_url is None
        assert status.source_url is None
        assert status.connected_clients == 0
        assert status.frames_served == 0

    def test_double_stop_no_crash(self):
        proxy = MJPEGProxy()
        proxy.stop()
        proxy.stop()
        assert proxy.active is False

    def test_status_after_stop_without_start(self):
        proxy = MJPEGProxy()
        info = proxy.stop()
        assert info.active is False

    @patch("kiln.streaming.HTTPServer")
    @patch("kiln.streaming.requests.get")
    @patch("kiln.streaming.threading.Thread")
    def test_start_sets_state(self, mock_thread_cls, mock_get, mock_server_cls):
        mock_get.return_value = MagicMock(ok=False)
        mock_thread = MagicMock()
        mock_thread_cls.return_value = mock_thread

        proxy = MJPEGProxy()
        info = proxy.start(
            source_url="http://test/stream",
            port=9999,
            printer_name="voron",
        )

        assert proxy.active is True
        assert info.active is True
        assert info.local_url == "http://localhost:9999/stream"
        assert info.source_url == "http://test/stream"
        assert info.printer_name == "voron"

        proxy._running = False
        proxy._stop_event.set()

    @patch("kiln.streaming.HTTPServer")
    @patch("kiln.streaming.requests.get")
    @patch("kiln.streaming.threading.Thread")
    def test_start_creates_server(self, mock_thread_cls, mock_get, mock_server_cls):
        mock_get.return_value = MagicMock(ok=False)
        mock_thread_cls.return_value = MagicMock()

        proxy = MJPEGProxy()
        proxy.start("http://test/stream", port=9999)

        mock_server_cls.assert_called_once()
        call_args = mock_server_cls.call_args
        assert call_args[0][0] == ("0.0.0.0", 9999)

        proxy._running = False
        proxy._stop_event.set()

    @patch("kiln.streaming.HTTPServer")
    @patch("kiln.streaming.requests.get")
    @patch("kiln.streaming.threading.Thread")
    def test_start_starts_threads(self, mock_thread_cls, mock_get, mock_server_cls):
        mock_get.return_value = MagicMock(ok=False)
        mock_thread = MagicMock()
        mock_thread_cls.return_value = mock_thread

        proxy = MJPEGProxy()
        proxy.start("http://test/stream", port=9999)

        # Should create 2 threads: server + reader
        assert mock_thread.start.call_count >= 1

        proxy._running = False
        proxy._stop_event.set()

    @patch("kiln.streaming.HTTPServer")
    @patch("kiln.streaming.requests.get")
    @patch("kiln.streaming.threading.Thread")
    def test_start_when_running_returns_current(self, mock_thread_cls, mock_get, mock_server_cls):
        mock_get.return_value = MagicMock(ok=False)
        mock_thread_cls.return_value = MagicMock()

        proxy = MJPEGProxy()
        info1 = proxy.start("http://test/stream", port=9999)
        info2 = proxy.start("http://other/stream", port=8888)

        # Second start should return same URL
        assert info1.local_url == info2.local_url

        proxy._running = False
        proxy._stop_event.set()

    @patch("kiln.streaming.HTTPServer")
    @patch("kiln.streaming.requests.get")
    @patch("kiln.streaming.threading.Thread")
    def test_stop_sets_inactive(self, mock_thread_cls, mock_get, mock_server_cls):
        mock_get.return_value = MagicMock(ok=False)
        mock_thread_cls.return_value = MagicMock()

        proxy = MJPEGProxy()
        proxy.start("http://test/stream", port=9999)
        info = proxy.stop()

        assert info.active is False
        assert proxy.active is False

    @patch("kiln.streaming.HTTPServer")
    @patch("kiln.streaming.requests.get")
    @patch("kiln.streaming.threading.Thread")
    def test_stop_calls_server_shutdown(self, mock_thread_cls, mock_get, mock_server_cls):
        mock_get.return_value = MagicMock(ok=False)
        mock_thread_cls.return_value = MagicMock()
        mock_server = MagicMock()
        mock_server_cls.return_value = mock_server

        proxy = MJPEGProxy()
        proxy.start("http://test/stream", port=9999)
        proxy.stop()

        mock_server.shutdown.assert_called_once()

    @patch("kiln.streaming.HTTPServer")
    @patch("kiln.streaming.requests.get")
    @patch("kiln.streaming.threading.Thread")
    def test_stop_clears_url(self, mock_thread_cls, mock_get, mock_server_cls):
        mock_get.return_value = MagicMock(ok=False)
        mock_thread_cls.return_value = MagicMock()

        proxy = MJPEGProxy()
        proxy.start("http://test/stream", port=9999)
        proxy.stop()
        status = proxy.status()
        assert status.local_url is None
        assert status.source_url is None

    @patch("kiln.streaming.HTTPServer")
    @patch("kiln.streaming.requests.get")
    @patch("kiln.streaming.threading.Thread")
    def test_frames_served_starts_at_zero(self, mock_thread_cls, mock_get, mock_server_cls):
        mock_get.return_value = MagicMock(ok=False)
        mock_thread_cls.return_value = MagicMock()

        proxy = MJPEGProxy()
        proxy.start("http://test/stream", port=9999)
        assert proxy.status().frames_served == 0

        proxy._running = False
        proxy._stop_event.set()

    @patch("kiln.streaming.HTTPServer")
    @patch("kiln.streaming.requests.get")
    @patch("kiln.streaming.threading.Thread")
    def test_status_printer_name(self, mock_thread_cls, mock_get, mock_server_cls):
        mock_get.return_value = MagicMock(ok=False)
        mock_thread_cls.return_value = MagicMock()

        proxy = MJPEGProxy()
        proxy.start("http://test/stream", port=9999, printer_name="ender3")
        status = proxy.status()
        assert status.printer_name == "ender3"

        proxy._running = False
        proxy._stop_event.set()

    @patch("kiln.streaming.HTTPServer")
    @patch("kiln.streaming.requests.get")
    @patch("kiln.streaming.threading.Thread")
    def test_connected_clients_starts_at_zero(self, mock_thread_cls, mock_get, mock_server_cls):
        mock_get.return_value = MagicMock(ok=False)
        mock_thread_cls.return_value = MagicMock()

        proxy = MJPEGProxy()
        proxy.start("http://test/stream", port=9999)
        assert proxy.status().connected_clients == 0

        proxy._running = False
        proxy._stop_event.set()

    @patch("kiln.streaming.HTTPServer")
    @patch("kiln.streaming.requests.get")
    @patch("kiln.streaming.threading.Thread")
    def test_start_stop_start(self, mock_thread_cls, mock_get, mock_server_cls):
        mock_get.return_value = MagicMock(ok=False)
        mock_thread_cls.return_value = MagicMock()

        proxy = MJPEGProxy()
        proxy.start("http://test/stream", port=9999)
        proxy.stop()
        assert proxy.active is False

        # Can restart
        proxy.start("http://test/stream", port=9999)
        assert proxy.active is True

        proxy._running = False
        proxy._stop_event.set()

    def test_status_uptime_when_not_running(self):
        proxy = MJPEGProxy()
        assert proxy.status().uptime_seconds == 0.0
