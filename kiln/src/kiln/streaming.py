"""Live-video relay: one printer connection, every viewer.

The relay reads frames from ONE upstream source and re-serves them as an
MJPEG stream on a local HTTP endpoint, so several viewers share a single
printer connection instead of each opening their own.  Two kinds of source
feed it today:

* :class:`HttpMjpegSource` — an upstream HTTP MJPEG URL (OctoPrint,
  Moonraker, a camera the user registered).
* a printer's own protocol, supplied by its adapter — a Bambu A1 / P1
  camera speaks TLS + framed JPEG on port 6000 and its adapter hands the
  relay a source that does (``BambuAdapter.camera_frame_source``).

The contract a source honours is :class:`FrameSource`: a ``kind`` and a
credential-free ``label`` for status, and ``frames(stop)`` yielding whole
JPEGs until asked to stop.  A source that cannot open raises
:class:`~kiln.printers.base.CameraStreamError` with the reason in words; the
relay records that reason on its status and retries with a backoff rather
than failing silently.

Fan-out is by frame SEQUENCE: each viewer waits for a frame newer than the
one it last sent, so a viewer that keeps up never misses a frame and a slow
one skips to the latest rather than falling behind.  Every served part and
the status carry the frame's AGE, and ``live`` is true only while a frame
arrived within the freshness budget — a frozen picture is never presented
as live, the way ``PrinterState.state_age_seconds`` keeps a stale reading
from posing as current.

Local-only by design: the relay reads the printer over the LAN and serves
on loopback.  The hosted server has no printer, so :meth:`MJPEGProxy.start`
refuses there with a message that says so.  Uses only stdlib
:mod:`http.server` and :mod:`threading` — no new dependencies.
"""

from __future__ import annotations

import collections
import contextlib
import logging
import os
import threading
import time
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, ClassVar, Protocol, runtime_checkable
from urllib.parse import parse_qs, urlparse

import requests

from kiln.printers.base import (
    RTSP_NOT_RELAYED_REASON,
    CameraStreamError,
    StreamCapability,
    redact_url_credentials,
)

logger = logging.getLogger(__name__)

_MAX_FRAME_SIZE: int = 10 * 1024 * 1024  # 10MB max frame size
_BOUNDARY = b"--kilnframe"
_CONTENT_TYPE = f"multipart/x-mixed-replace; boundary={_BOUNDARY.decode()}"

#: A frame older than this is not "live" — the picture may be frozen.  Ten
#: seconds covers the slowest camera Kiln relays (a P1 pushes about one
#: frame every two seconds) with room for a hiccup, and is short enough
#: that a printer that stopped answering reads as stale within one poll.
LIVE_BUDGET_SECONDS: float = 10.0

#: Window the measured frame rate is averaged over.
_FPS_WINDOW_SECONDS: float = 10.0

#: Wait between reconnect attempts after the upstream fails or ends.
_RECONNECT_BACKOFF_SECONDS: float = 2.0

#: The relay reads the printer over the LAN and serves on loopback; the
#: hosted server has neither.  One wording for every door.
LOCAL_ONLY_MESSAGE = (
    "Live video plays only on the computer connected to the printer: Kiln's "
    "relay reads the camera over your local network and serves it on this "
    "machine. The hosted server has no printer to read. Run Kiln locally "
    "(kiln serve, or the MCP server on that machine) to watch live; "
    "snapshots and monitoring still work here."
)


# ---------------------------------------------------------------------------
# What the relay tells Kiln about live video — closed vocabularies
# ---------------------------------------------------------------------------
#
# The relay is the one place that knows whether a printer's camera really
# gave live video: which feed it opened, whether frames arrived, how fast,
# and why the printer refused.  It records that once per session through
# ``kiln.daily_stats.record_video_outcome`` as four tokens, and these tuples
# are the only words each token may take.  This module is their one home:
# the camera check and the dashboard read them from here.

#: The feed the relay read, or tried to.
VIDEO_CHANNELS: tuple[str, ...] = (
    "bambu_port6000", "http_mjpeg", "rtsp", "webrtc", "none", "other",
)

#: Whose camera: the printer's own, a camera the user registered that is
#: served from the printer's own address, or one elsewhere.
VIDEO_SOURCES: tuple[str, ...] = ("printer", "user_same_host", "user_other")

#: What happened, at most once per session each.
VIDEO_EVENTS: tuple[str, ...] = (
    "start", "live",
    "fps_lt1", "fps_1to5", "fps_5to15", "fps_15up",
    "refused_access", "refused_unreachable", "refused_no_stream",
    "refused_rtsp", "refused_webrtc", "refused_other",
)

#: The shape of a stream path on the printer's own address, recorded with
#: its port as ``addr_<port>_<class>`` when a user-registered camera there
#: goes live.  A class, never the path: the text could carry a token.
ADDRESS_PATH_CLASSES: tuple[str, ...] = (
    "action_stream", "webcam_action_stream", "stream", "video", "root", "other",
)

#: What each path class looks like, for saying an observed address in words
#: ("port 8080 at /?action=stream").  ``other`` has no shape: it is, by
#: definition, none of these.  A round-trip test pins every shape to the
#: classifier above, so the words cannot drift from the rule.
ADDRESS_PATH_SHAPES: dict[str, str] = {
    "action_stream": "/?action=stream",
    "webcam_action_stream": "/webcam/?action=stream",
    "stream": "/stream",
    "video": "/video",
    "root": "/",
}

#: What a camera check found at an address (see :mod:`kiln.camera_check`).
CHECK_RESULTS: tuple[str, ...] = (
    "mjpeg", "jpeg", "webrtc_signalling", "html", "http_error", "unreachable", "other",
)

#: Appended to a refusal on a printer type that has a camera check.
CHECK_OFFER = "Ask Kiln to check this printer's camera to see what it serves."

#: Frames a session must receive before its rate is measured once.
_FPS_MIN_FRAMES = 5

#: Plan-level refusals already recorded, keyed by day: a print monitor asks
#: for video on every poll, and one refused printer must read as one
#: refusal, not a count of polls.
_PLAN_RECORDED: set[tuple[str, ...]] = set()
_PLAN_RECORDED_LOCK = threading.Lock()


def video_model_for(printer_name: str | None) -> str:
    """The config-declared model of the printer a relay was aimed at, as a key token.

    ``"unknown"`` when nothing resolves — never a guess, never a raise.
    """
    try:
        from kiln.daily_stats import video_model_token
        from kiln.printer_model_resolver import resolve_printer_model_for

        return video_model_token(resolve_printer_model_for(printer_name))
    except Exception:  # noqa: BLE001 — telemetry never breaks a start
        return "unknown"


def _fps_bucket(fps: float) -> str:
    if fps < 1:
        return "fps_lt1"
    if fps < 5:
        return "fps_1to5"
    if fps < 15:
        return "fps_5to15"
    return "fps_15up"


def _channel_token(channel: str | None) -> str:
    if not channel:
        return "none"
    return channel if channel in VIDEO_CHANNELS else "other"


def _hostname(value: object) -> str | None:
    """The bare, lowercased host of a URL or host string, or ``None``."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    parsed = urlparse(text if "://" in text else f"//{text}")
    return (parsed.hostname or "").lower() or None


def _adapter_host(adapter: Any) -> str | None:
    """The printer's own host, from the attribute its adapter keeps it in."""
    for holder in (adapter, getattr(adapter, "_backend", None)):
        if holder is None:
            continue
        for attr in ("_host", "host", "_base_url"):
            host = _hostname(getattr(holder, attr, None))
            if host:
                return host
    return None


def _address_event(url: str) -> str | None:
    """``addr_<port>_<path class>`` for a stream URL, or ``None``."""
    parsed = urlparse(url)
    try:
        port = parsed.port or {"http": 80, "https": 443, "rtsp": 554}.get(parsed.scheme.lower())
    except ValueError:
        return None
    if not port or not 0 < port < 65536:
        return None
    path = (parsed.path or "/").rstrip("/") or "/"
    action_stream = "stream" in parse_qs(parsed.query).get("action", [])
    if path == "/" and action_stream:
        cls = "action_stream"
    elif path == "/webcam" and action_stream:
        cls = "webcam_action_stream"
    elif path == "/stream":
        cls = "stream"
    elif path == "/video":
        cls = "video"
    elif path == "/" and not parsed.query:
        cls = "root"
    else:
        cls = "other"
    return f"addr_{port}_{cls}"


def _video_source(adapter: Any) -> tuple[str, str | None]:
    """(source token, address event) for what the relay would read."""
    camera = getattr(adapter, "external_camera", None)
    if camera is None:
        return "printer", None
    stream = getattr(camera, "stream_url", None)
    camera_host = _hostname(stream)
    if camera_host and camera_host == _adapter_host(adapter):
        return "user_same_host", _address_event(stream)
    return "user_other", None


def _record_outcome(model: str, channel: str, source: str, event: str) -> None:
    try:
        from kiln.daily_stats import record_video_outcome

        record_video_outcome(model, channel, source, event)
    except Exception:  # noqa: BLE001 — telemetry never breaks the relay
        logger.debug("video outcome not recorded", exc_info=True)


def _record_plan_refusal(printer_name: str | None, channel: str, source: str, event: str) -> None:
    """Record a refusal decided before any connection, once per process-day."""
    today = date.today().isoformat()
    key = (today, printer_name or "", channel, source, event)
    with _PLAN_RECORDED_LOCK:
        if key in _PLAN_RECORDED:
            return
        stale = {k for k in _PLAN_RECORDED if k[0] != today}
        _PLAN_RECORDED.difference_update(stale)
        _PLAN_RECORDED.add(key)
    _record_outcome(video_model_for(printer_name), channel, source, event)


@dataclass(frozen=True)
class VideoObservation:
    """What :func:`plan_relay` knew about a session, stamped on its source.

    The relay reads it from the source it is handed, so every door that
    starts the relay from a plan records the session without knowing the
    counter exists.  The model is resolved when a session starts, not per
    poll.
    """

    printer_name: str | None
    channel: str
    source: str
    address_event: str | None = None


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------


@runtime_checkable
class FrameSource(Protocol):
    """What the relay reads: whole JPEG frames, until asked to stop."""

    kind: str
    label: str

    def frames(self, stop: threading.Event) -> Iterator[bytes]: ...


class HttpMjpegSource:
    """An upstream HTTP MJPEG stream (OctoPrint, Moonraker, a user camera)."""

    kind = "http_mjpeg"

    def __init__(self, url: str, *, timeout: float = 10.0) -> None:
        self._url = url
        self._timeout = timeout
        self.label = redact_url_credentials(url) or url

    def frames(self, stop: threading.Event) -> Iterator[bytes]:
        try:
            resp = requests.get(self._url, stream=True, timeout=self._timeout)
        except requests.RequestException as exc:
            raise CameraStreamError(
                f"The camera stream did not answer: {exc.__class__.__name__}.",
                code="CAMERA_UNREACHABLE",
            ) from exc
        if not resp.ok:
            resp.close()
            raise CameraStreamError(
                f"The camera stream answered HTTP {resp.status_code}.",
                code="CAMERA_REFUSED" if resp.status_code in (401, 403) else "CAMERA_UNREACHABLE",
            )
        try:
            buf = bytearray()
            in_frame = False
            for chunk in resp.iter_content(chunk_size=4096):
                if stop.is_set():
                    return
                buf.extend(chunk)
                if len(buf) > _MAX_FRAME_SIZE:
                    logger.warning("MJPEG frame buffer exceeded %d bytes, resetting", _MAX_FRAME_SIZE)
                    buf = bytearray()
                    in_frame = False
                    continue
                while True:
                    if not in_frame:
                        start = buf.find(b"\xff\xd8")
                        if start == -1:
                            # Keep last byte in case marker is split
                            if len(buf) > 1:
                                buf = buf[-1:]
                            break
                        buf = buf[start:]
                        in_frame = True
                    end = buf.find(b"\xff\xd9")
                    if end == -1:
                        break
                    frame = bytes(buf[: end + 2])
                    buf = buf[end + 2 :]
                    in_frame = False
                    yield frame
        except requests.RequestException as exc:
            raise CameraStreamError(
                f"The camera stream dropped: {exc.__class__.__name__}.",
                code="CAMERA_UNREACHABLE",
            ) from exc
        finally:
            resp.close()


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------


@dataclass
class StreamInfo:
    """Status information for the relay.

    ``frame_age_seconds`` is how long ago the newest frame arrived from the
    printer; ``live`` is whether that is within :data:`LIVE_BUDGET_SECONDS`.
    ``measured_fps`` is the rate the printer actually pushed over the last
    few seconds — measured, never a spec-sheet number.  ``last_error`` is the
    upstream's most recent refusal in words, cleared by the next frame.
    """

    active: bool
    local_url: str | None = None
    source_url: str | None = None
    source_kind: str | None = None
    printer_name: str | None = None
    connected_clients: int = 0
    frames_served: int = 0
    frames_received: int = 0
    uptime_seconds: float = 0.0
    frame_age_seconds: float | None = None
    measured_fps: float | None = None
    live: bool = False
    last_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# The door's plan — one helper every door calls
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RelayPlan:
    """What a door does with a start request: relay ``source``, or refuse.

    ``source`` is ``None`` exactly when the relay has nothing to carry;
    ``code`` and ``message`` are then the refusal every door reports the
    same way.  ``capability`` rides both outcomes so the reply always says
    what the printer can do.
    """

    capability: StreamCapability
    source: FrameSource | None = None
    code: str | None = None
    message: str | None = None
    #: True when the printer type has a camera check a user can run
    #: (``PrinterAdapter.camera_check_ids``); a refusal then says so.
    check_available: bool = False


def plan_relay(adapter: Any, printer_name: str | None = None) -> RelayPlan:
    """Decide, for *adapter*, whether the relay can start and on what.

    The one place the tool, the CLI and the monitor doors resolve a start
    request, so a backend that learns a new protocol is picked up by every
    door at once.  A user-registered RTSP camera keeps its own refusal code
    (``RTSP_NOT_PROXIED``) because callers already read it.

    It is also where a refusal decided before any connection is recorded
    (once per process-day), and where a relayable source is stamped with a
    :class:`VideoObservation` for the relay to record the session against.
    ``printer_name`` names the machine the start was aimed at; omitted, it
    means the default printer.
    """
    capability = _capability_of(adapter)
    source_token, address_event = _video_source(adapter)
    check_ids = getattr(adapter, "camera_check_ids", ())
    check_available = isinstance(check_ids, tuple) and len(check_ids) > 0

    def _refuse(code: str, message: str, channel: str, event: str) -> RelayPlan:
        _record_plan_refusal(printer_name, channel, source_token, event)
        if check_available:
            message = f"{message} {CHECK_OFFER}"
        return RelayPlan(capability, None, code, message, check_available=check_available)

    if not capability.available:
        channel = _channel_token(capability.channel)
        event = {"rtsp": "refused_rtsp", "webrtc": "refused_webrtc"}.get(
            capability.channel or "", "refused_no_stream"
        )
        if capability.channel == "rtsp" and getattr(adapter, "external_camera", None) is not None:
            return _refuse("RTSP_NOT_PROXIED", RTSP_NOT_RELAYED_REASON, channel, event)
        return _refuse(
            "NO_STREAM",
            capability.reason or "Webcam streaming not available for this printer.",
            channel,
            event,
        )
    source = adapter.frame_source() if hasattr(adapter, "frame_source") else None
    if not isinstance(source, FrameSource):
        # A duck-typed adapter (a mock, a third-party backend) answers the
        # older contract only: its stream URL is the source.
        url = adapter.get_stream_url()
        source = HttpMjpegSource(url) if isinstance(url, str) and url else None
    if source is None:
        return _refuse(
            "NO_STREAM", "Webcam streaming not available for this printer.",
            "none", "refused_no_stream",
        )
    # A source that cannot carry the observation relays unrecorded.
    with contextlib.suppress(AttributeError):
        source.video_observation = VideoObservation(  # type: ignore[attr-defined]
            printer_name=printer_name,
            channel=_channel_token(getattr(source, "kind", None) or capability.channel),
            source=source_token,
            address_event=address_event,
        )
    return RelayPlan(capability, source, check_available=check_available)


def _capability_of(adapter: Any) -> StreamCapability:
    """*adapter*'s capability answer, or one derived from its stream URL.

    Only a real :class:`StreamCapability` is trusted — a mocked or
    third-party adapter without the method is answered from the older
    contract (``get_stream_url``), the way ``adapter_has_camera`` trusts
    only a real bool.
    """
    answer = adapter.stream_capability() if hasattr(adapter, "stream_capability") else None
    if isinstance(answer, StreamCapability):
        return answer
    url = adapter.get_stream_url()
    if not isinstance(url, str) or not url:
        return StreamCapability(False, None, "Webcam streaming not available for this printer.")
    if url.lower().startswith(("rtsp://", "rtsps://")):
        return StreamCapability(False, "rtsp", RTSP_NOT_RELAYED_REASON)
    return StreamCapability(True, "http_mjpeg")


# ---------------------------------------------------------------------------
# MJPEG proxy
# ---------------------------------------------------------------------------


class MJPEGProxy:
    """Background HTTP server that relays one frame source to many viewers.

    Usage::

        proxy = MJPEGProxy()
        proxy.start("http://octoprint.local/webcam/?action=stream", port=8081)
        # Stream available at http://localhost:8081/stream
        proxy.stop()

    Or with a printer's own source::

        proxy.start(frame_source=adapter.frame_source(), port=8081)
    """

    def __init__(self) -> None:
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._frame_source: FrameSource | None = None
        self._printer_name: str | None = None
        self._started_at: float | None = None
        self._port: int = 8081
        self._lock = threading.RLock()

        # Shared frame state: one condition, a sequence number, and the
        # newest frame.  Viewers wait for a sequence newer than their last.
        self._cond = threading.Condition()
        self._latest_frame: bytes | None = None
        self._latest_seq: int = 0
        self._latest_at: float | None = None  # monotonic
        self._recent: collections.deque[float] = collections.deque()
        self._frames_received: int = 0
        self._last_error: str | None = None

        self._connected_clients: int = 0
        self._frames_served: int = 0
        self._running = False
        self._stop_event = threading.Event()

        # Upstream reader thread
        self._reader_thread: threading.Thread | None = None

        # The current session's observation (see VideoObservation) and what
        # it has already recorded — each event at most once per session.
        self._observation: VideoObservation | None = None
        self._session_model: str = "unknown"
        self._session_live = False
        self._session_refused = False
        self._session_fps_recorded = False
        self._session_first_frame_at: float | None = None

    @property
    def active(self) -> bool:
        return self._running

    @property
    def printer_name(self) -> str | None:
        return self._printer_name

    def start(
        self,
        source_url: str | None = None,
        port: int = 8081,
        printer_name: str | None = None,
        *,
        host: str | None = None,
        frame_source: FrameSource | None = None,
    ) -> StreamInfo:
        """Start the relay.

        Args:
            source_url: Upstream MJPEG stream URL (wrapped in an
                :class:`HttpMjpegSource`).  Either this or *frame_source*.
            port: Local port to serve on.
            printer_name: Name of the printer (for status reporting).
            host: Bind address.  Defaults to ``KILN_STREAM_HOST`` env var,
                then ``127.0.0.1``.
            frame_source: A source speaking the printer's own protocol.

        A relay already running for the SAME printer is returned as is; one
        running for another printer is stopped first, so a start can never
        hand back a different machine's picture.

        Raises:
            RuntimeError: on the hosted multi-tenant server, which has no
                printer to read — see :data:`LOCAL_ONLY_MESSAGE`.
            ValueError: with neither a URL nor a source.
        """
        from kiln.runtime_env import is_hosted_multitenant

        if is_hosted_multitenant():
            raise RuntimeError(LOCAL_ONLY_MESSAGE)
        if frame_source is None:
            if not source_url:
                raise ValueError("start() needs a source_url or a frame_source")
            frame_source = HttpMjpegSource(source_url)

        with self._lock:
            if self._running:
                if self._printer_name == printer_name:
                    return self.status()
                self.stop()

            observation = getattr(frame_source, "video_observation", None)
            self._observation = observation if isinstance(observation, VideoObservation) else None
            self._session_model = (
                video_model_for(self._observation.printer_name) if self._observation else "unknown"
            )
            self._session_live = False
            self._session_refused = False
            self._session_fps_recorded = False
            self._session_first_frame_at = None
            self._frame_source = frame_source
            self._printer_name = printer_name
            self._port = port
            self._started_at = time.time()
            self._frames_served = 0
            self._frames_received = 0
            self._connected_clients = 0
            self._latest_frame = None
            self._latest_seq = 0
            self._latest_at = None
            self._recent.clear()
            self._last_error = None
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

                # Start one behind the newest so a viewer sees the latest
                # frame at once instead of waiting for the next push.
                last_sent = max(0, proxy._latest_seq - 1)
                try:
                    while proxy._running:
                        with proxy._cond:
                            if proxy._latest_seq <= last_sent or proxy._latest_frame is None:
                                proxy._cond.wait(timeout=1.0)
                                continue
                            frame = proxy._latest_frame
                            seq = proxy._latest_seq
                            age = time.monotonic() - (proxy._latest_at or time.monotonic())
                        last_sent = seq
                        try:
                            self.wfile.write(_BOUNDARY + b"\r\n")
                            self.wfile.write(b"Content-Type: image/jpeg\r\n")
                            self.wfile.write(f"Content-Length: {len(frame)}\r\n".encode())
                            self.wfile.write(f"X-Frame-Sequence: {seq}\r\n".encode())
                            self.wfile.write(f"X-Frame-Age-Seconds: {age:.3f}\r\n\r\n".encode())
                            self.wfile.write(frame)
                            self.wfile.write(b"\r\n")
                            self.wfile.flush()
                            with proxy._lock:
                                proxy._frames_served += 1
                        except (BrokenPipeError, ConnectionResetError, OSError):
                            break
                finally:
                    with proxy._lock:
                        proxy._connected_clients = max(0, proxy._connected_clients - 1)

            def log_message(self, format: str, *args: Any) -> None:
                # Suppress default HTTP logging
                pass

        bind_host = host or os.environ.get("KILN_STREAM_HOST", "127.0.0.1")
        server = ThreadingHTTPServer((bind_host, port), Handler)
        server.daemon_threads = True
        self._server = server
        self._thread = threading.Thread(
            target=lambda: server.serve_forever(poll_interval=0.1),
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

        logger.info("MJPEG relay started on port %d <- %s", port, frame_source.label)
        self._record_session("start")
        return self.status()

    def stop(self) -> StreamInfo:
        """Stop the relay and release the printer connection."""
        info = self.status()
        with self._lock:
            self._running = False

        # Signal any waiting threads
        self._stop_event.set()
        with self._cond:
            self._cond.notify_all()

        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None

        if self._reader_thread is not None:
            self._reader_thread.join(timeout=5.0)
            self._reader_thread = None

        with self._lock:
            self._frame_source = None
            self._started_at = None
            self._connected_clients = 0

        info.active = False
        info.live = False
        logger.info("MJPEG relay stopped")
        return info

    def status(self) -> StreamInfo:
        """Return current relay status, frame age included."""
        with self._lock, self._cond:
            uptime = 0.0
            if self._started_at and self._running:
                uptime = time.time() - self._started_at
            now = time.monotonic()
            age = None if self._latest_at is None else max(0.0, now - self._latest_at)
            fps = None
            if len(self._recent) >= 2:
                span = self._recent[-1] - self._recent[0]
                if span > 0:
                    fps = round((len(self._recent) - 1) / span, 2)
            source = self._frame_source
            return StreamInfo(
                active=self._running,
                local_url=(f"http://localhost:{self._port}/stream" if self._running else None),
                source_url=source.label if source is not None else None,
                source_kind=source.kind if source is not None else None,
                printer_name=self._printer_name,
                connected_clients=self._connected_clients,
                frames_served=self._frames_served,
                frames_received=self._frames_received,
                uptime_seconds=round(uptime, 1),
                frame_age_seconds=None if age is None else round(age, 3),
                measured_fps=fps,
                live=bool(self._running and age is not None and age <= LIVE_BUDGET_SECONDS),
                last_error=self._last_error,
            )

    # -- upstream ---------------------------------------------------------

    def _publish(self, frame: bytes) -> None:
        now = time.monotonic()
        first = False
        fps_event: str | None = None
        with self._cond:
            self._latest_frame = frame
            self._latest_seq += 1
            self._latest_at = now
            self._frames_received += 1
            self._last_error = None
            self._recent.append(now)
            while self._recent and now - self._recent[0] > _FPS_WINDOW_SECONDS:
                self._recent.popleft()
            if not self._session_live:
                self._session_live = first = True
                self._session_first_frame_at = now
            elif (
                not self._session_fps_recorded
                and self._frames_received >= _FPS_MIN_FRAMES
                and self._session_first_frame_at is not None
                and now > self._session_first_frame_at
            ):
                # Over the whole session rather than the status window, so
                # a camera slower than one frame per two seconds still gets
                # its rate measured.
                self._session_fps_recorded = True
                fps_event = _fps_bucket(
                    (self._frames_received - 1) / (now - self._session_first_frame_at)
                )
            self._cond.notify_all()
        # Recorded outside the frame lock: a disk write must never hold up
        # the viewers waiting on this frame.
        if first:
            self._record_session("live")
            if self._observation is not None and self._observation.address_event:
                self._record_session(self._observation.address_event)
        if fps_event:
            self._record_session(fps_event)

    def _note_error(self, message: str) -> None:
        with self._cond:
            self._last_error = message

    #: A source's refusal code → the event recorded for the session.
    _REFUSAL_EVENTS: ClassVar[dict[str, str]] = {
        "CAMERA_REFUSED": "refused_access",
        "CAMERA_UNREACHABLE": "refused_unreachable",
    }

    def _note_refusal(self, exc: CameraStreamError) -> None:
        """Name the refusal on status, and record the session's first one."""
        self._note_error(str(exc))
        with self._cond:
            if self._session_live or self._session_refused:
                return
            self._session_refused = True
        self._record_session(self._REFUSAL_EVENTS.get(exc.code, "refused_other"))

    def _record_session(self, event: str) -> None:
        """Record one event for the current session, when it has an observation."""
        observation = self._observation
        if observation is None:
            return
        _record_outcome(self._session_model, observation.channel, observation.source, event)

    def _read_upstream(self) -> None:
        """Background thread: read the source, publish, reconnect on loss."""
        source = self._frame_source
        if source is None:
            return
        while self._running:
            try:
                for frame in source.frames(self._stop_event):
                    if not self._running:
                        break
                    if len(frame) > _MAX_FRAME_SIZE:
                        logger.warning("Dropping oversized frame (%d bytes)", len(frame))
                        continue
                    self._publish(frame)
                if self._running:
                    self._note_error("The camera stream ended; reconnecting.")
            except CameraStreamError as exc:
                logger.debug("relay source refused: %s", exc)
                self._note_refusal(exc)
            except Exception as exc:  # noqa: BLE001 — keep the relay alive, name the fault
                logger.exception("Unexpected error in relay reader")
                self._note_error(f"Relay reader error: {exc}")
            if self._running:
                self._stop_event.wait(_RECONNECT_BACKOFF_SECONDS)
