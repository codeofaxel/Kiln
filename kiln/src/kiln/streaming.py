"""MJPEG streaming proxy for webcam feeds.

Reads an MJPEG stream from the upstream printer (OctoPrint or Moonraker)
and re-serves it over a local HTTP endpoint so that multiple clients can
connect without putting extra load on the printer.

Uses only stdlib :mod:`http.server` and :mod:`threading` — no new
dependencies.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import requests

logger = logging.getLogger(__name__)

_MAX_FRAME_SIZE: int = 10 * 1024 * 1024  # 10MB max frame size
_BOUNDARY = b"--kilnframe"
_CONTENT_TYPE = f"multipart/x-mixed-replace; boundary={_BOUNDARY.decode()}"


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------


@dataclass
class StreamInfo:
    """Status information for the MJPEG proxy."""

    active: bool
    local_url: str | None = None
    source_url: str | None = None
    printer_name: str | None = None
    connected_clients: int = 0
    frames_served: int = 0
    uptime_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# MJPEG proxy
# ---------------------------------------------------------------------------


class MJPEGProxy:
    """Background HTTP server that proxies an upstream MJPEG stream.

    Usage::

        proxy = MJPEGProxy()
        proxy.start("http://octoprint.local/webcam/?action=stream", port=8081)
        # Stream available at http://localhost:8081/stream
        proxy.stop()
    """

    def __init__(self) -> None:
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._source_url: str | None = None
        self._printer_name: str | None = None
        self._started_at: float | None = None
        self._port: int = 8081
        self._lock = threading.RLock()

        # Shared state for the handler
        self._latest_frame: bytes | None = None
        self._frame_lock = threading.Lock()
        self._frame_event = threading.Event()
        self._connected_clients: int = 0
        self._frames_served: int = 0
        self._running = False
        self._stop_event = threading.Event()

        # Upstream reader thread
        self._reader_thread: threading.Thread | None = None

    @property
    def active(self) -> bool:
        return self._running

    def start(
        self,
        source_url: str,
        port: int = 8081,
        printer_name: str | None = None,
        *,
        host: str | None = None,
    ) -> StreamInfo:
        """Start the proxy server.

        Args:
            source_url: Upstream MJPEG stream URL.
            port: Local port to serve on.
            printer_name: Name of the printer (for status reporting).
            host: Bind address.  Defaults to ``KILN_STREAM_HOST`` env var,
                then ``127.0.0.1``.

        Returns:
            :class:`StreamInfo` with the local URL.
        """
        with self._lock:
            if self._running:
                return self.status()

            self._source_url = source_url
            self._printer_name = printer_name
            self._port = port
            self._started_at = time.time()
            self._frames_served = 0
            self._connected_clients = 0
            self._running = True
            self._stop_event.clear()

        proxy = self  # closure ref

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path != "/stream":
                    self.send_response(404)
                    self.end_headers()
                    self.wfile.write(b"Not Found. Use /stream")
                    return

                self.send_response(200)
                self.send_header("Content-Type", _CONTENT_TYPE)
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()

                with proxy._lock:
                    proxy._connected_clients += 1

                try:
                    while proxy._running:
                        proxy._frame_event.wait(timeout=5.0)
                        proxy._frame_event.clear()

                        with proxy._frame_lock:
                            frame = proxy._latest_frame

                        if frame is None:
                            continue

                        try:
                            self.wfile.write(_BOUNDARY + b"\r\n")
                            self.wfile.write(b"Content-Type: image/jpeg\r\n")
                            self.wfile.write(f"Content-Length: {len(frame)}\r\n\r\n".encode())
                            self.wfile.write(frame)
                            self.wfile.write(b"\r\n")
                            self.wfile.flush()
                            with proxy._lock:
                                proxy._frames_served += 1
                        except (BrokenPipeError, ConnectionResetError):
                            break
                finally:
                    with proxy._lock:
                        proxy._connected_clients = max(
                            0,
                            proxy._connected_clients - 1,
                        )

            def log_message(self, format: str, *args: Any) -> None:
                # Suppress default HTTP logging
                pass

        bind_host = host or os.environ.get("KILN_STREAM_HOST", "127.0.0.1")
        self._server = HTTPServer((bind_host, port), Handler)
        self._thread = threading.Thread(
            target=lambda: self._server.serve_forever(poll_interval=0.1),
            daemon=True,
            name="kiln-mjpeg-server",
        )
        self._thread.start()

        # Start upstream reader
        self._reader_thread = threading.Thread(
            target=self._read_upstream,
            daemon=True,
            name="kiln-mjpeg-reader",
        )
        self._reader_thread.start()

        logger.info(
            "MJPEG proxy started on port %d -> %s",
            port,
            source_url,
        )
        return self.status()

    def stop(self) -> StreamInfo:
        """Stop the proxy server and clean up."""
        info = self.status()
        with self._lock:
            self._running = False

        # Signal any waiting threads
        self._stop_event.set()
        self._frame_event.set()

        if self._server is not None:
            self._server.shutdown()
            self._server = None

        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None

        if self._reader_thread is not None:
            self._reader_thread.join(timeout=5.0)
            self._reader_thread = None

        with self._lock:
            self._source_url = None
            self._started_at = None
            self._connected_clients = 0

        info.active = False
        logger.info("MJPEG proxy stopped")
        return info

    def status(self) -> StreamInfo:
        """Return current proxy status."""
        with self._lock:
            uptime = 0.0
            if self._started_at and self._running:
                uptime = time.time() - self._started_at
            return StreamInfo(
                active=self._running,
                local_url=(f"http://localhost:{self._port}/stream" if self._running else None),
                source_url=self._source_url,
                printer_name=self._printer_name,
                connected_clients=self._connected_clients,
                frames_served=self._frames_served,
                uptime_seconds=round(uptime, 1),
            )

    def _read_upstream(self) -> None:
        """Background thread that reads MJPEG frames from the upstream."""
        if not self._source_url:
            return

        while self._running:
            try:
                resp = requests.get(
                    self._source_url,
                    stream=True,
                    timeout=10,
                )
                if not resp.ok:
                    logger.warning(
                        "Upstream stream returned %d",
                        resp.status_code,
                    )
                    self._stop_event.wait(2.0)
                    continue

                buf = bytearray()
                in_frame = False

                for chunk in resp.iter_content(chunk_size=4096):
                    if not self._running:
                        break
                    buf.extend(chunk)

                    if len(buf) > _MAX_FRAME_SIZE:
                        logger.warning("MJPEG frame buffer exceeded %d bytes, resetting", _MAX_FRAME_SIZE)
                        buf = bytearray()
                        in_frame = False
                        continue

                    while True:
                        if not in_frame:
                            # Look for JPEG start marker
                            start = buf.find(b"\xff\xd8")
                            if start == -1:
                                # Keep last byte in case marker is split
                                if len(buf) > 1:
                                    buf = buf[-1:]
                                break
                            buf = buf[start:]
                            in_frame = True

                        # Look for JPEG end marker
                        end = buf.find(b"\xff\xd9")
                        if end == -1:
                            break

                        # Extract complete JPEG frame
                        frame = bytes(buf[: end + 2])
                        buf = buf[end + 2 :]
                        in_frame = False

                        if len(frame) > _MAX_FRAME_SIZE:
                            logger.warning("Dropping oversized MJPEG frame (%d bytes)", len(frame))
                            continue

                        with self._frame_lock:
                            self._latest_frame = frame
                        self._frame_event.set()

            except requests.RequestException:
                logger.debug("Upstream stream error, reconnecting...", exc_info=True)
                self._stop_event.wait(2.0)
            except Exception:
                logger.exception("Unexpected error in MJPEG reader")
                self._stop_event.wait(2.0)
