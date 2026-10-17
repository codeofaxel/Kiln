"""The camera check: what a printer's likely camera addresses actually serve.

When the relay has nothing to carry for a printer, the useful question is
whether its camera is really unavailable or answering somewhere Kiln did not
look.  A printer type that knows where its camera tends to answer declares
those addresses (:meth:`~kiln.printers.base.PrinterAdapter.camera_probes`),
each with the basis for trying it; this module opens each one once and says
what came back.

Two readings of a camera address, two rules.

The CHECK says what an address SERVES.  Only when asked:
``webcam_stream(action="check")`` and ``kiln stream --check`` run it, never a
start, a poll or a monitor.  It only reads — one GET per address, no
redirect followed, no body sent — each read is bounded in bytes and in time,
and the connection is closed before the next address is tried.  It
registers nothing: a stream it finds is reported with the step the user can
take.  Each result is counted as ``<model>|check|<probe_id>|<result>``
(:func:`kiln.daily_stats.record_camera_check`): which address was tried, by
its id, and which class of answer came back — never the address.

The HANDSHAKE says only whether a camera ANSWERS.  Setup runs it once, after
the printer itself has answered, so the person is told at the moment they
would ask (:meth:`~kiln.printers.base.PrinterAdapter.camera_handshake`).
Connecting is not using: it reads headers, never a frame — no frame is
read, returned, stored or shown — each address is bounded to
:data:`~kiln.printers.base.CAMERA_HANDSHAKE_ADDRESS_TIMEOUT` seconds and the
whole handshake to :data:`~kiln.printers.base.CAMERA_HANDSHAKE_BUDGET`, every
connection is closed, and nothing on the printer is switched on.  A camera
that has to be started to be asked is reported as not checked, never probed
(``answered=None``), and nothing is counted.
"""

from __future__ import annotations

import http.client
import logging
import re
import time
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urlsplit

import requests
import urllib3.exceptions

from kiln import daily_stats, streaming
from kiln.printers.base import (
    CameraHandshake,
    ExternalCamera,
    StreamCapability,
    _is_rtsp,
    redact_url_credentials,
)

logger = logging.getLogger(__name__)

#: A probe id names which address was tried, as a token the heartbeat can
#: carry; it can never spell the address itself.
_PROBE_ID_RE = re.compile(r"^[a-z0-9_]{3,32}$")

#: Upper bound on one socket read while reading a page.
_READ_CHUNK = 4096

#: What a page that sets up WebRTC video contains, matched case-insensitively.
_WEBRTC_MARKER = b"rtcpeerconnection"

NO_CAMERA_CHECK_MESSAGE = (
    "Kiln has no camera check for this printer type: it knows of no address "
    "this printer's camera might answer on. If you know your camera's "
    "address, register it with the printer (camera_stream_url) to watch it."
)


@dataclass(frozen=True)
class CameraProbe:
    """One address a printer type's camera may answer on.

    ``url`` is on the printer's own host.  ``basis`` says in plain words where
    the address comes from and whether that source is the maker's own or a
    community one.  ``note``, when set, is added to every result for this
    probe: something a user should know about checking this address at all.
    """

    probe_id: str
    url: str
    basis: str
    note: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.probe_id, str) or not _PROBE_ID_RE.match(self.probe_id):
            raise ValueError(f"A camera probe id must match {_PROBE_ID_RE.pattern}: {self.probe_id!r}")


@dataclass(frozen=True)
class CameraCheckResult:
    """What one address answered.

    ``result`` is a word from :data:`kiln.streaming.CHECK_RESULTS` and
    ``detail`` says it in a sentence; ``http_status`` and ``content_type`` are
    ``None`` when nothing answered.
    """

    probe_id: str
    result: str
    detail: str
    http_status: int | None
    content_type: str | None
    url: str
    basis: str

    def __post_init__(self) -> None:
        if self.result not in streaming.CHECK_RESULTS:
            raise ValueError(f"Not a camera check result: {self.result!r}")

    def to_dict(self) -> dict[str, Any]:
        """The result as a reply carries it, with any password in the address redacted."""
        payload = asdict(self)
        payload["url"] = redact_url_credentials(self.url) or self.url
        return payload


def classify_camera_address(
    url: str,
    *,
    timeout: float = 3.0,
    max_bytes: int = 65536,
    session: requests.Session | None = None,
) -> tuple[str, str, int | None, str | None]:
    """Open *url* once and say what it serves: ``(result, detail, http_status, content_type)``.

    One streamed GET: no redirect followed, no body sent, no proxy from the
    environment (a camera on the local network is never reached through
    one).  Where the status or content type decides the answer the body is
    never read, so a live stream is closed as soon as its headers arrive;
    otherwise at most *max_bytes* of it are read, within *timeout* seconds of
    the headers, looking for a page that sets up a WebRTC peer connection —
    ``max_bytes=0`` reads headers only.  Every socket wait is bounded by
    *timeout*, and the connection is always closed.  Never raises for
    anything the address does.

    *session* is an adapter's own session for an address that needs its
    credentials (a printer's authenticated camera endpoint); it is left open
    for the adapter.  Without one a fresh session is opened and closed here.
    """
    try:
        scheme = urlsplit(url).scheme.lower()
    except (AttributeError, TypeError, ValueError):
        scheme = ""
    if scheme not in ("http", "https"):
        if scheme:
            return "other", f"This address uses {scheme}, not http, so the check did not open it.", None, None
        return "other", "This is not a web address, so the check did not open it.", None, None

    own_session = session is None
    if session is None:
        session = requests.Session()
        session.trust_env = False
    response: requests.Response | None = None
    try:
        try:
            response = session.get(
                url,
                stream=True,
                timeout=(timeout, timeout),
                allow_redirects=False,
                headers={"Accept-Encoding": "identity"},
            )
        except requests.Timeout:
            return "unreachable", f"Nothing answered within {timeout:g} seconds.", None, None
        except requests.exceptions.SSLError:
            return "other", "The secure connection to this address failed, so the check could not read it.", None, None
        except requests.ConnectionError:
            return (
                "unreachable",
                "Nothing answered at this address: the connection was refused or dropped, "
                "or the name did not resolve.",
                None,
                None,
            )
        except requests.RequestException as exc:
            return "other", f"The check could not open this address ({exc.__class__.__name__}).", None, None
        return _classify_response(response, timeout=timeout, max_bytes=max_bytes)
    finally:
        if response is not None:
            response.close()
        if own_session:
            session.close()


def _classify_response(
    response: requests.Response, *, timeout: float, max_bytes: int
) -> tuple[str, str, int | None, str | None]:
    status = response.status_code
    content_type = _media_type(response.headers.get("Content-Type"))
    if status >= 400:
        return "http_error", f"It answered with an error (HTTP {status}).", status, content_type
    if 300 <= status < 400:
        return (
            "other",
            f"It answered with a redirect (HTTP {status}), which the check does not follow.",
            status,
            content_type,
        )
    if content_type == "multipart/x-mixed-replace":
        return (
            "mjpeg",
            "It answered with a live stream of images (multipart MJPEG), the kind of feed Kiln's relay carries.",
            status,
            content_type,
        )
    if content_type == "image/jpeg":
        return "jpeg", "It answered with a single still image, not a video stream.", status, content_type
    body = _read_bounded(response, timeout=timeout, max_bytes=max_bytes)
    if _WEBRTC_MARKER in body.lower():
        return (
            "webrtc_signalling",
            "It answered with a page that sets up a WebRTC video connection, which Kiln's relay does not carry.",
            status,
            content_type,
        )
    if content_type == "text/html":
        return "html", "It answered with a web page, not a video stream.", status, content_type
    return (
        "other",
        f"It answered with something that is neither a video stream nor a web page ({content_type or 'no content type'}).",
        status,
        content_type,
    )


def _media_type(header: str | None) -> str | None:
    """``"text/html; charset=utf-8"`` -> ``"text/html"``; ``None`` when absent."""
    if not header:
        return None
    return header.split(";", 1)[0].strip().lower() or None


def _read_bounded(response: requests.Response, *, timeout: float, max_bytes: int) -> bytes:
    """At most *max_bytes* of the body, read within *timeout* seconds.

    A plain ``read(n)`` waits until it has all *n* bytes, so a server sending
    one byte at a time could hold it for as long as it liked while every
    single socket wait stayed inside the timeout.  ``read1`` returns after at
    most one socket read; an older urllib3 without it reads one byte at a
    time, which gives the same bound.  The body arrives as sent — the request
    asked for no compression, and nothing is decompressed here.
    """
    raw = response.raw
    read1 = getattr(raw, "read1", None)
    deadline = time.monotonic() + timeout
    body = bytearray()
    try:
        while len(body) < max_bytes and time.monotonic() < deadline:
            if read1 is not None:
                chunk = read1(min(_READ_CHUNK, max_bytes - len(body)))
            else:
                chunk = raw.read(1)
            if not chunk:
                break
            body.extend(chunk)
    except (urllib3.exceptions.HTTPError, http.client.HTTPException, OSError):
        # The headers already said what answered; a body that stops partway
        # is classified on what arrived.
        logger.debug("camera check body read ended early", exc_info=True)
    return bytes(body[:max_bytes])


def _probes_of(adapter: Any) -> list[CameraProbe]:
    """*adapter*'s probes; a missing method or an answer that is not a list is none."""
    ask = getattr(adapter, "camera_probes", None)
    if not callable(ask):
        return []
    probes = ask()
    if not isinstance(probes, list):
        return []
    return [probe for probe in probes if isinstance(probe, CameraProbe)]


def run_camera_checks(adapter: Any, *, printer_name: str | None = None) -> list[CameraCheckResult]:
    """Check every address *adapter*'s printer type offers, and record each result.

    Run only when a user asks for it.  Addresses are tried one after another,
    never at once: several can lead to the same camera service on the
    printer, and some printers limit how many video connections they accept.
    Each result is counted once under the model of *printer_name*'s printer
    (the default printer when omitted), by probe id and result.
    """
    probes = _probes_of(adapter)
    if not probes:
        return []
    model = streaming.video_model_for(printer_name)
    results: list[CameraCheckResult] = []
    for probe in probes:
        result, detail, status, content_type = classify_camera_address(probe.url)
        if probe.note:
            detail = f"{detail} {probe.note}"
        results.append(
            CameraCheckResult(
                probe_id=probe.probe_id,
                result=result,
                detail=detail,
                http_status=status,
                content_type=content_type,
                url=probe.url,
                basis=probe.basis,
            )
        )
        daily_stats.record_camera_check(model, probe.probe_id, result)
    return results


#: How a reader at each door keeps a live stream the check found.  The words
#: differ because the doors do: an agent registers a printer through a tool,
#: while a person saves one with ``kiln auth``, which replaces the printer's
#: whole saved entry and so needs its details again.
_KEEP_STREAM_STEPS = {
    "tool": (
        "You can register {url} as this printer's camera (register_printer with "
        'camera_stream_url), then start video with webcam_stream action "start". '
        "Kiln has not registered anything."
    ),
    "cli": (
        "You can save {url} as this printer's camera: run kiln auth for this printer "
        "again with its details and --camera-stream-url {url}, then kiln stream. "
        "Kiln has not saved anything."
    ),
}


def summarize_camera_checks(
    results: list[CameraCheckResult], adapter: Any = None, *, door: str
) -> tuple[str, str | None]:
    """A check reply's ``summary`` and ``next_step``.

    The summary reads the same at every door.  ``next_step`` is set only when
    an address served a live stream, in the words of *door* (``"tool"`` or
    ``"cli"``, see :data:`_KEEP_STREAM_STEPS`); nothing is registered here.
    With no results, *adapter*'s own reason for having no camera address is
    added when it gives one.
    """
    if not results:
        summary = "The printer gave Kiln no camera address to check, so nothing was checked."
        reason = _own_camera_reason(adapter)
        return (f"{summary} {reason}" if reason else summary), None

    tried = f"Kiln checked {len(results)} camera address{'' if len(results) == 1 else 'es'} on this printer"
    stream = next((r for r in results if r.result == "mjpeg"), None)
    if stream is not None:
        url = stream.to_dict()["url"]
        return f"{tried} and found a live video stream at {url}.", _KEEP_STREAM_STEPS[door].format(url=url)
    if all(r.result == "unreachable" for r in results):
        return f"{tried}, and nothing answered.", None
    summary = f"{tried}, and none served a live video stream Kiln's relay can carry."
    if any(r.result == "webrtc_signalling" for r in results):
        summary += " One answered with a page that sets up a WebRTC video connection, which the relay does not carry."
    return summary, None


def _own_camera_reason(adapter: Any) -> str | None:
    """Why the printer's own camera gave no address, in its adapter's words.

    Asked only when no user camera is registered: with one, the adapter's
    answer describes the user's camera, not the printer's.
    """
    if adapter is None or getattr(adapter, "external_camera", None) is not None:
        return None
    ask = getattr(adapter, "stream_capability", None)
    if not callable(ask):
        return None
    try:
        capability = ask()
    except Exception:  # noqa: BLE001 — a courtesy sentence must not fail the check
        logger.debug("stream_capability failed during a camera check", exc_info=True)
        return None
    if not isinstance(capability, StreamCapability):
        return None
    reason = capability.reason
    return reason if isinstance(reason, str) and reason.strip() else None


# ---------------------------------------------------------------------------
# The handshake: does a camera answer, learned without a frame
# ---------------------------------------------------------------------------

#: Said when a printer type knows no address its camera might answer on.
NO_CAMERA_HANDSHAKE_DETAIL = (
    "Kiln knows no address this printer's camera might answer on, so there was nothing to ask."
)


def _camera_answer(result: str, status: int | None, content_type: str | None) -> tuple[bool, str]:
    """Whether a classified answer is a camera answering, and what came back in a few words.

    A camera answers with an image or a live stream.  A web page, an error
    page or silence is the printer's host answering, or nothing — not a
    camera — and the words say which, so a "not answering" line carries
    the evidence.
    """
    if result == "mjpeg":
        return True, "a live stream"
    if result == "jpeg" or (content_type or "").startswith("image/"):
        return True, "an image"
    if result == "unreachable":
        return False, "no reply"
    if result == "http_error":
        return False, f"an error (HTTP {status})"
    if result in ("html", "webrtc_signalling"):
        return False, "a web page, not a camera"
    if status is None:
        return False, "no answer Kiln could read"
    return False, f"not a camera ({content_type or 'no content type'})"


def handshake_address(
    url: str, *, timeout: float, session: requests.Session | None = None
) -> tuple[bool, str]:
    """Headers only: ``(answered, what)`` for one http(s) address.

    The same bounded GET as the check, with no body read at all
    (``max_bytes=0``): a live stream is hung up on as its headers arrive,
    and no frame is read or returned.  *session* carries an adapter's own
    credentials for an endpoint that needs them.
    """
    result, _detail, status, content_type = classify_camera_address(
        url, timeout=timeout, max_bytes=0, session=session
    )
    return _camera_answer(result, status, content_type)


def handshake_external_camera(camera: ExternalCamera, *, timeout: float) -> CameraHandshake:
    """Whether the camera the user supplied answers, headers only.

    The still address is asked when there is one (one request, one image),
    else the stream address (hung up on at its headers).  An rtsp(s) camera
    is not asked: Kiln's only RTSP reader opens the stream to cut a frame,
    which is using the camera, not a handshake.
    """
    url = camera.snapshot_url or camera.stream_url
    if not url:
        return CameraHandshake(None, "external", "No camera address is registered.")
    if _is_rtsp(url):
        return CameraHandshake(
            None,
            "external",
            "The camera you supplied is an RTSP stream, and Kiln has no RTSP client "
            "that asks without opening the stream, so it was not checked.",
        )
    answered, what = handshake_address(url, timeout=timeout)
    if answered:
        return CameraHandshake(True, "external", f"The camera you supplied answered with {what}.")
    return CameraHandshake(
        False,
        "external",
        f"The camera you supplied is not answering ({what}); it may be off, or the address may be wrong.",
    )


def handshake_probes(
    probes: list[CameraProbe], *, budget: float, address_timeout: float
) -> CameraHandshake:
    """Ask each probe address in turn, headers only, until one answers as a camera.

    Addresses are tried one at a time (several can lead to one camera
    service, and some printers cap video connections), each within
    *address_timeout* seconds, and the whole round within *budget*: an
    address the budget leaves no time for is reported untried, never
    skipped silently.  Nothing is counted.
    """
    deadline = time.monotonic() + budget
    tried: list[str] = []
    for index, probe in enumerate(probes):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            untried = len(probes) - index
            tail = f" {untried} more address{'' if untried == 1 else 'es'} went untried in the time allowed."
            break
        answered, what = handshake_address(probe.url, timeout=min(address_timeout, remaining))
        if answered:
            return CameraHandshake(
                True, probe.probe_id, f"The printer's camera answered with {what} at {probe.probe_id}."
            )
        tried.append(f"{probe.probe_id}: {what}")
    else:
        tail = ""
    return CameraHandshake(
        False,
        ",".join(entry.split(":", 1)[0] for entry in tried),
        f"Nothing answered as a camera ({'; '.join(tried)}).{tail}",
    )


#: How each setup door names the way to add a camera the user points at the
#: bed: an agent passes a parameter, a person passes a flag.
_ADD_CAMERA_HINTS = {
    "tool": "a camera you point at the bed can be added with camera_stream_url",
    "cli": "a camera you point at the bed can be added with kiln auth --camera-stream-url",
}


def describe_handshake(handshake: CameraHandshake, *, door: str) -> str:
    """The one line a setup door shows for *handshake*, in the words of *door*.

    ``"tool"`` or ``"cli"`` (:data:`_ADD_CAMERA_HINTS`).  Never an address.
    """
    hint = _ADD_CAMERA_HINTS[door]
    external = handshake.where == "external"
    if handshake.answered is True:
        return "Camera: answering (the camera you supplied)." if external else "Camera: answering (built-in)."
    if handshake.answered is False:
        if external:
            return f"Camera: {handshake.detail} Kept anyway."
        return f"Camera: not answering at {handshake.where}; {hint}."
    if external:
        return f"Camera: not checked. {handshake.detail}"
    if not handshake.where:
        return f"Camera: {handshake.detail} If it has one, {hint}."
    return f"Camera: Kiln cannot check this printer's camera without starting it. {handshake.detail}"


def camera_setup_line(adapter: Any, *, door: str) -> tuple[CameraHandshake | None, str]:
    """Run *adapter*'s handshake for a setup door: the result and its one line.

    The one helper every door that saves a printer calls, after the printer
    itself has answered.  An adapter without the handshake (a stand-in in a
    test, an older plugin) gets ``(None, "")`` — the door shows nothing —
    and a handshake that raises is reported as not checked, never as a
    failed setup.
    """
    ask = getattr(adapter, "camera_handshake", None)
    if not callable(ask):
        return None, ""
    try:
        handshake = ask()
    except Exception:  # noqa: BLE001 — setup has already succeeded; a camera line must not undo it
        logger.debug("camera handshake failed during setup", exc_info=True)
        return None, "Camera: not checked (the handshake failed unexpectedly)."
    if not isinstance(handshake, CameraHandshake):
        return None, ""
    return handshake, describe_handshake(handshake, door=door)
