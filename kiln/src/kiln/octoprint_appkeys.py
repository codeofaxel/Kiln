"""Ask OctoPrint for an API key instead of having a person copy one.

OctoPrint comes with an Application Keys plugin: an app asks for a key, the
person clicks Allow inside OctoPrint, and OctoPrint hands the app a key made
for it.  From OctoPrint 1.8 the request also carries a small approval page
the person can open directly, so they never have to find a settings menu.

Three rules of OctoPrint's shape everything here:

* It forgets a request nobody has asked about for five seconds, so whoever
  starts one has to keep asking, about once a second, until it is decided.
* It allows only a few requests a minute from one place, so Kiln keeps ONE
  request open per printer and answers every later question from it rather
  than starting another.
* It keeps a decision for ten minutes, which is as long as Kiln waits.

Two ways in.  A terminal waits in place (:func:`wait_for_key`), printing the
approval page while it does.  An agent cannot wait for a person mid-call, so
:func:`ask_in_background` starts the request, keeps it alive on a thread, and
answers each later call with where it stands; the call that finds it granted
takes the key.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

import requests

logger = logging.getLogger(__name__)

#: The name OctoPrint shows the person on its Allow / Deny prompt.
APP_NAME = "Kiln"
#: How often Kiln asks whether the person has decided.  OctoPrint drops a
#: request it has not been asked about for five seconds.
POLL_INTERVAL_S = 1.0
#: How long Kiln waits for a decision: as long as OctoPrint keeps one.
WAIT_LIMIT_S = 600.0
#: Consecutive failed checks after which the request is taken as lost.  At
#: one check a second this passes OctoPrint's five-second limit.
_MISSES_BEFORE_LOST = 6
_HTTP_TIMEOUT_S = 5.0

_PROBE_PATH = "/plugin/appkeys/probe"
_REQUEST_PATH = "/plugin/appkeys/request"
_APPROVE_PATH = "/plugin/appkeys/auth"


class AppKeyError(Exception):
    """OctoPrint could not be asked for a key; the message says why."""


@dataclass(frozen=True)
class KeyRequest:
    """One open request for a key."""

    #: The OctoPrint address Kiln asked, scheme included.
    host: str
    app_token: str
    #: The approval page to open, or ``None`` before OctoPrint 1.8, where the
    #: prompt appears only inside OctoPrint's own web page.
    approve_url: str | None

    @property
    def poll_url(self) -> str:
        return f"{self.host}{_REQUEST_PATH}/{self.app_token}"


def base_url(host: str) -> str:
    """*host* as a URL with a scheme and no trailing slash."""
    cleaned = host.strip().rstrip("/")
    if "://" not in cleaned:
        cleaned = f"http://{cleaned}"
    return cleaned


def how_to_approve(request: KeyRequest) -> str:
    """The sentence that tells a person what to do with *request*."""
    if request.approve_url:
        return f"open {request.approve_url}, sign in to OctoPrint if it asks, and click Allow"
    return (
        "open OctoPrint in a browser where you are signed in; a request from "
        f"{APP_NAME} appears there, and you click Allow"
    )


def supported(host: str, *, session: requests.Session | None = None) -> bool:
    """True when this OctoPrint can hand out keys this way."""
    http = session or requests
    try:
        response = http.get(f"{base_url(host)}{_PROBE_PATH}", timeout=_HTTP_TIMEOUT_S)
    except requests.RequestException as exc:
        logger.debug("OctoPrint key probe failed for %s: %s", host, exc)
        return False
    return response.status_code == 204


def start(host: str, *, session: requests.Session | None = None) -> KeyRequest:
    """Ask OctoPrint for a key for Kiln.  The person still has to allow it."""
    http = session or requests
    url = base_url(host)
    try:
        response = http.post(f"{url}{_REQUEST_PATH}", json={"app": APP_NAME}, timeout=_HTTP_TIMEOUT_S)
    except requests.RequestException as exc:
        raise AppKeyError(f"Could not reach OctoPrint at {url}: {exc}") from exc
    if response.status_code == 429:
        raise AppKeyError(
            "OctoPrint allows only a few key requests a minute and refused this one. "
            "Wait a minute and try again, or paste a key instead."
        )
    if response.status_code != 201:
        raise AppKeyError(f"OctoPrint at {url} did not accept a key request (HTTP {response.status_code}).")
    try:
        body = response.json()
    except ValueError as exc:
        raise AppKeyError(f"OctoPrint at {url} answered a key request with something Kiln cannot read.") from exc
    token = str(body.get("app_token") or "").strip()
    if not token:
        raise AppKeyError(f"OctoPrint at {url} answered a key request without a request token.")
    # The approval page is built from the address Kiln used rather than
    # copied from the reply: OctoPrint names it from the headers it saw,
    # which behind a proxy can be an address the person cannot open.
    approve_url = f"{url}{_APPROVE_PATH}/{token}" if body.get("auth_dialog") else None
    return KeyRequest(host=url, app_token=token, approve_url=approve_url)


def check(request: KeyRequest, *, session: requests.Session | None = None) -> tuple[str, str | None]:
    """Where *request* stands: ``("pending", None)``, ``("granted", key)``, or
    ``("refused", None)`` -- denied, or forgotten by OctoPrint.

    Raises :class:`requests.RequestException` when OctoPrint does not answer.
    """
    http = session or requests
    response = http.get(request.poll_url, timeout=_HTTP_TIMEOUT_S)
    if response.status_code == 202:
        return "pending", None
    if response.status_code == 200:
        try:
            key = str(response.json().get("api_key") or "").strip()
        except ValueError:
            key = ""
        return ("granted", key) if key else ("refused", None)
    return "refused", None


def wait_for_key(
    request: KeyRequest,
    *,
    limit_s: float = WAIT_LIMIT_S,
    on_wait: Callable[[], None] | None = None,
    session: requests.Session | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> str | None:
    """Ask about *request* until the person decides; the key, or ``None``."""
    deadline = clock() + limit_s
    misses = 0
    while clock() < deadline:
        try:
            state, key = check(request, session=session)
            misses = 0
        except requests.RequestException as exc:
            misses += 1
            logger.debug("OctoPrint key check failed (%d in a row): %s", misses, exc)
            if misses >= _MISSES_BEFORE_LOST:
                return None
            state, key = "pending", None
        if state == "granted":
            return key
        if state == "refused":
            return None
        if on_wait is not None:
            on_wait()
        sleep(POLL_INTERVAL_S)
    return None


# ---------------------------------------------------------------------------
# The agent's way in: one request per printer, kept alive between calls.
# ---------------------------------------------------------------------------


@dataclass
class _Open:
    request: KeyRequest
    state: str = "pending"
    key: str | None = None


#: One open request per OctoPrint address.  Process memory only: a key
#: request is for the person at this machine, and the hosted server refuses
#: the door that fills this (``register_printer``) before it runs.
_OPEN: dict[str, _Open] = {}
_LOCK = threading.Lock()


def _keep_alive(entry: _Open) -> None:
    key: str | None = None
    try:
        key = wait_for_key(entry.request)
    except Exception:  # an open request must never stay "pending" forever
        logger.exception("OctoPrint key request for %s stopped unexpectedly", entry.request.host)
    with _LOCK:
        entry.state, entry.key = ("granted", key) if key else ("refused", None)


def ask_in_background(host: str) -> dict[str, str | None]:
    """Where Kiln's request for a key from the OctoPrint at *host* stands,
    starting one if none is open.

    Returns ``state`` -- ``granted`` (with ``api_key``), ``pending`` (with
    ``how`` and ``approve_url``), ``refused``, ``unsupported``, or ``error``
    (with ``detail``).  A granted or refused answer is given once; the next
    call starts afresh.
    """
    from kiln.runtime_env import is_hosted_multitenant

    if is_hosted_multitenant():
        return {"state": "error", "detail": "A key request has to come from the computer next to the printer."}
    url = base_url(host)
    with _LOCK:
        entry = _OPEN.get(url)
        if entry is not None and entry.state != "pending":
            del _OPEN[url]
            return {"state": entry.state, "api_key": entry.key}
        if entry is not None:
            return {
                "state": "pending",
                "how": how_to_approve(entry.request),
                "approve_url": entry.request.approve_url,
            }
    if not supported(url):
        return {"state": "unsupported"}
    try:
        request = start(url)
    except AppKeyError as exc:
        return {"state": "error", "detail": str(exc)}
    entry = _Open(request=request)
    with _LOCK:
        # Two calls racing past the check above: keep the first request and
        # let the second lapse; OctoPrint forgets it in five seconds.
        existing = _OPEN.setdefault(url, entry)
    if existing is entry:
        threading.Thread(target=_keep_alive, args=(entry,), name="octoprint-appkey", daemon=True).start()
    return {
        "state": "pending",
        "how": how_to_approve(existing.request),
        "approve_url": existing.request.approve_url,
    }
