"""Web->printer bridge: dial OUT to Kiln's relay so the web can drive this engine.

Opt-in. When the user enables web control, this holds a websocket to
``api.kiln3d.com`` and runs relay-safe tool calls (printer status,
slice-and-print, monitor, pause/cancel) locally against THIS machine's
printers — the very same tools the MCP path already runs. The browser never
reaches the local network; it talks only to ``api.kiln3d.com``, which forwards
to this held-open socket (tenant-matched, server-side). No printer tech is
reinvented here — this is transport + local execution of existing tools.

Why dial OUT (not listen): an outbound socket opens no inbound port, needs no
firewall change, and sidesteps the browser's local-network restrictions that
make a listening localhost bridge fail in Safari. Auth is the user's license;
the relay server enforces tenant isolation + the ``RELAY_SAFE_TOOLS`` allow-list,
so a call can only ever reach its own account's machine.

The core (:func:`handle_relay_request`) is pure and injected with its tool caller
+ artifact fetcher, so it is unit-tested without a socket, the cloud, or a
printer. The network loop wires the real dependencies.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import tempfile
import threading
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_RELAY_URL = "wss://api.kiln3d.com/api/bridge/connect"
_DEFAULT_API_URL = "https://api.kiln3d.com"

# Liveness the running bridge advertises to a small JSON file, so
# ``kiln bridge status`` can tell "on and connected" from "on but reconnecting"
# from "off" WITHOUT opening its own socket.  Written best-effort by whichever
# process runs the loop (a manual ``start`` or the login service).
_STATE_FILE = "~/.kiln/bridge.state"

# Injected dependency shapes.
ToolCaller = Callable[[str, dict], Any]          # (tool_name, args) -> result
ArtifactFetcher = Callable[[str], str]           # cloud token -> local file path


def handle_relay_request(
    req: dict,
    *,
    call_tool: ToolCaller,
    fetch_artifact: ArtifactFetcher,
) -> dict:
    """Execute one relayed tool call locally and build the wire response.

    Pure: every side effect goes through the two injected callables, so the
    routing/print-resolution logic is testable in isolation. Never raises — a
    failure becomes ``{"ok": False, "error": ...}`` so one bad call can't drop
    the socket for every other in-flight call.

    Print path: the web can't hand us a local file, so a ``slice_and_print``
    carrying a ``cloud_artifact_token`` is resolved HERE — fetch the geometry
    from the cloud to a temp file, then run the normal ``slice_and_print`` on it.
    Every other tool is a straight passthrough.

    The yes: a relayed start may carry ``print_authority`` — the record the
    hosted server made when a person pressed Approve for these bytes on this
    printer, or the delegation an agent is printing under.  The local gate
    needs a person's yes and never takes one from an argument, so the block
    is taken OUT of the args here and turned into the consent for this one
    call (see :func:`_consent_from_authority`), and only after the bytes
    that arrived hash to the bytes that were approved.  Bytes that differ
    are refused before any tool runs: a consent by file name would let a
    re-generated model print under the old approval.
    """
    request_id = req.get("request_id")
    tool = str(req.get("tool_name") or "")
    args = dict(req.get("args") or {})
    reset = None
    try:
        token = args.pop("cloud_artifact_token", None)
        authority = args.pop("print_authority", None)
        if tool == "slice_and_print" and token:
            args["input_path"] = fetch_artifact(str(token))
        if authority:
            consent = _consent_from_authority(
                authority, file_name=str(args.get("input_path") or args.get("model_path") or ""),
                printer_name=args.get("printer_name"),
            )
            if consent is not None:
                from kiln.print_consent import set_consent

                reset = set_consent(consent)
        result = call_tool(tool, args)
        return {"request_id": request_id, "ok": True, "result": result}
    except Exception as exc:  # deliberately broad — one call must not kill the ws
        logger.info("relay tool %r failed: %s", tool, exc)
        return {
            "request_id": request_id,
            "ok": False,
            "error": {"message": str(exc), "tool": tool},
        }
    finally:
        if reset is not None:
            from kiln.print_consent import reset_consent

            reset_consent(reset)


class NotTheApprovedBytes(RuntimeError):
    """The file that arrived is not the file the person approved."""


def _sha256_of(path: str) -> str:
    import hashlib

    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _consent_from_authority(block: Any, *, file_name: str, printer_name: Any):
    """The consent a relayed authority block stands for, or ``None``.

    Only two kinds are known — ``approval`` (a person's yes to one print)
    and ``delegation`` (an account's yes to a named agent for a while) —
    and each maps to the consent source public Kiln already grades A on
    the hosted server.  Anything else is dropped, and the call runs with
    no consent, so the gate refuses it in its own words rather than this
    module inventing a yes.  Raises :class:`NotTheApprovedBytes` when the
    approved hash and the fetched bytes disagree, which the caller turns
    into a refused call.
    """
    from kiln.print_consent import (
        SCOPE_FLEET,
        SOURCE_HOSTED_APPROVAL,
        SOURCE_HOSTED_DELEGATION,
        PrintConsent,
    )

    if not isinstance(block, dict):
        return None
    kind = str(block.get("kind") or "")
    source = {"approval": SOURCE_HOSTED_APPROVAL, "delegation": SOURCE_HOSTED_DELEGATION}.get(kind)
    record_id = str(block.get("id") or "")
    grantor = str(block.get("grantor") or "")
    if source is None or not record_id or not grantor:
        return None
    expected = str(block.get("file_sha256") or "").strip().lower()
    if file_name and expected:
        actual = _sha256_of(file_name)
        shortest = min(len(actual), len(expected))
        if shortest < 32 or actual[:shortest] != expected[:shortest]:
            raise NotTheApprovedBytes(
                "not started: the file that arrived is not the one that was approved "
                f"({record_id}); approve the print again from the page that shows it."
            )
    # The identity is WHO SAID GO, then whose yes it rested on: the person
    # starting under their own approval is the account; an agent starting
    # under a delegation — or under the one print the person approved at
    # its asking — is "agent under account#record", never the person.
    said_go_by = str(block.get("said_go_by") or grantor)
    identity = f"{grantor}#{record_id}" if said_go_by == grantor else f"{said_go_by} under {grantor}#{record_id}"
    if kind == "approval":
        scope = None
    else:
        printers = block.get("printers")
        if printers == SCOPE_FLEET:
            scope = SCOPE_FLEET
        elif isinstance(printers, (list, tuple)) and printers:
            scope = tuple(str(p) for p in printers)
        else:
            scope = None
    until = block.get("until")
    # The printer is the one the CALL names, or none: the gate matches a
    # consent against the name the call used, and the hosted server has
    # already held the record to the printer it was made for.
    return PrintConsent(
        tool="bridge relay",
        file_name=file_name or str(block.get("file_name") or ""),
        printer_name=str(printer_name) if printer_name else None,
        source=source,
        scope=scope,
        expires_at=float(until) if isinstance(until, (int, float)) else None,
        identity=identity,
        door=str(block.get("door") or ""),
    )


# ---------------------------------------------------------------------------
# Default (production) dependencies
# ---------------------------------------------------------------------------


def _default_tool_caller() -> ToolCaller:
    """Invoke a registered Kiln tool by name — the same functions the MCP runs.

    Reuses the server's tool registry so every safety gate the tool already
    carries (preflight, auto-print-off default, validation) applies unchanged.

    ``ensure_runtime_config()`` is what makes that reuse real.  Importing
    ``kiln.server`` registers the tools but leaves the printer globals at
    their import-time defaults; the MCP server resolves them in ``main()``
    and the REST API in ``create_app()``, neither of which runs here.
    Without this call every printer-touching relay tool answers "No printer
    configured" on a machine whose ``~/.kiln/config.yaml`` is perfectly
    good — the browser sees "no printer" forever.
    """
    from kiln import server as _server

    _server.ensure_runtime_config()

    def call_tool(name: str, args: dict) -> Any:
        tool = _server.mcp._tool_manager._tools.get(name)
        if tool is None:
            raise ValueError(f"tool {name!r} is not available on this machine")
        return tool.fn(**args)

    return call_tool


def _default_artifact_fetcher(get_bearer: Callable[[], str]) -> ArtifactFetcher:
    """Fetch a saved make's geometry from the cloud to a local temp file.

    The web only holds a cloud reference; the bridge pulls the actual mesh with
    the user's credential so it has a real local path for ``slice_and_print``.

    Takes a *getter* rather than a token because a sign-in session expires
    hourly while the bridge runs for days: a string captured at construction
    would 401 on the first fetch after expiry, turning a print into an
    unexplained failure.
    """
    api = os.environ.get("KILN_API_URL", _DEFAULT_API_URL).rstrip("/")

    def fetch(token: str) -> str:
        url = f"{api}/api/artifact/{token}"
        request = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {get_bearer()}"}
        )
        with urllib.request.urlopen(request, timeout=30) as resp:  # noqa: S310
            data = resp.read()
        fd, path = tempfile.mkstemp(suffix=".stl", prefix="kiln-relay-")
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        return path

    return fetch


def observe_addresses(
    api: str, bearer: str, nonce: str, *, post: Callable[[int], bool] | None = None,
) -> dict[str, bool]:
    """``POST /api/bridge/observe`` once per address family, each carrying
    *nonce*.  Returns ``{"v4": bool, "v6": bool}`` — which sides answered.
    *post* is the per-family request (injected by tests); the default binds
    a plain HTTPS connection to one family so the relay sees THAT side."""
    import socket

    do_post = post or (lambda family: _post_observe(api, bearer, nonce, family))
    shown: dict[str, bool] = {}
    for label, family in (("v4", socket.AF_INET), ("v6", socket.AF_INET6)):
        try:
            shown[label] = bool(do_post(family))
        except Exception:  # noqa: BLE001 — a family this machine lacks is not an error
            logger.debug("bridge: no %s route to the relay", label, exc_info=True)
            shown[label] = False
    return shown


def _post_observe(api: str, bearer: str, nonce: str, family: int) -> bool:
    """One observe request over one address family.  True on a 2xx."""
    import http.client
    import socket
    import ssl
    from urllib.parse import urlsplit

    parts = urlsplit(api)
    host = parts.hostname or ""
    secure = parts.scheme == "https"
    port = parts.port or (443 if secure else 80)
    infos = socket.getaddrinfo(host, port, family, socket.SOCK_STREAM)
    if not infos:
        return False
    sockaddr = infos[0][4]

    class _Bound(http.client.HTTPSConnection if secure else http.client.HTTPConnection):
        # http.client picks whichever family resolves first; this one is
        # told which side of the machine to speak from.
        def connect(self) -> None:
            raw = socket.socket(family, socket.SOCK_STREAM)
            raw.settimeout(self.timeout)
            raw.connect(sockaddr)
            if secure:
                context = getattr(self, "_context", None) or ssl.create_default_context()
                raw = context.wrap_socket(raw, server_hostname=host)
            self.sock = raw

    conn = _Bound(host, port, timeout=10)
    try:
        conn.request(
            "POST", "/api/bridge/observe", body=json.dumps({"nonce": nonce}),
            headers={"Authorization": f"Bearer {bearer}", "Content-Type": "application/json"},
        )
        return 200 <= conn.getresponse().status < 300
    finally:
        conn.close()


def _read_license() -> str:
    """The bearer this machine presents to the relay, or ``""`` if there is none.

    Routes through :func:`kiln.auth_session.resolve_api_bearer` — the one
    resolver every authenticated Kiln API caller uses — so a
    ``KILN_LICENSE_KEY`` wins, and otherwise the ``kiln signin`` / ``kiln
    pair`` session is used and transparently refreshed near expiry.

    It has to be that resolver and not a local re-read.  This function used
    to check only the env var and ``license_key`` in ``~/.kiln/config.yaml``
    — neither of which sign-in writes — so a fully signed-in machine was
    told "Bridge: signed out.  Sign in first: kiln signin", by the one
    command that could not fix it.  ``kiln signin --help`` promises the
    opposite in writing: the rest of the CLI picks the session up with no
    license key needed.  The relay accepts a Supabase JWT, so the session
    was always a valid bearer; the bridge was simply the surface that never
    learned to read it.

    The ``config.yaml`` fallback stays last so an operator who put a license
    key in the file keeps working.
    """
    try:
        from kiln.auth_session import resolve_api_bearer

        token = resolve_api_bearer().token.strip()
        if token:
            return token
    except Exception:  # never let auth resolution break the bridge
        logger.debug("session bearer resolution failed", exc_info=True)

    try:
        import yaml  # kiln already depends on PyYAML

        cfg_path = os.path.expanduser("~/.kiln/config.yaml")
        with open(cfg_path, encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
        return str(cfg.get("license_key") or "").strip()
    except Exception:
        return ""


def _running_version() -> str:
    """The Kiln version THIS process is actually running.

    Frozen at import, which is the point: a ``pip install --upgrade`` while the
    daemon runs changes the files on disk and changes nothing in here.  Read
    deliberately from the imported module rather than the installed
    distribution's metadata, because the two answer different questions —
    metadata says what pip last put on disk, and what a long-lived daemon is
    serving is what it loaded.

    One helper for both readers (the relay handshake and the state file) so the
    version the server sees and the version ``kiln bridge status`` reports can
    never drift into disagreeing about one process.
    """
    try:
        from kiln import __version__ as _v  # noqa: PLC0415

        return str(_v)
    except Exception:  # noqa: BLE001 -- version introspection is never fatal
        return ""


def _device_fingerprint() -> str:
    """Stable per-machine id the activation-cap accounting expects (env or MAC)."""
    fp = os.environ.get("KILN_DEVICE_FINGERPRINT", "").strip()
    if fp:
        return fp
    import uuid

    return f"bridge-{uuid.getnode():x}"


# ---------------------------------------------------------------------------
# Network loop
# ---------------------------------------------------------------------------


class BridgeClient:
    """Holds the outbound relay websocket and dispatches inbound calls.

    Concurrency: each inbound request runs in a worker thread
    (``asyncio.to_thread``) so a slow ``slice_and_print`` never blocks status /
    monitor polls on the same socket, and its reply is sent whenever it's ready.
    Reconnects with exponential backoff so a dropped link self-heals.
    """

    def __init__(
        self,
        *,
        license_key: str | None = None,
        relay_url: str | None = None,
        call_tool: ToolCaller | None = None,
        fetch_artifact: ArtifactFetcher | None = None,
    ) -> None:
        #: An explicitly supplied bearer pins the credential (tests, an
        #: operator passing a license); otherwise it is resolved fresh on
        #: every use — see :meth:`_bearer`.
        self._pinned_license = license_key
        self._url = relay_url or os.environ.get("KILN_RELAY_URL", _DEFAULT_RELAY_URL)
        self._call_tool = call_tool or _default_tool_caller()
        self._fetch_artifact = fetch_artifact or _default_artifact_fetcher(self._bearer)
        self._stop = False

    def _bearer(self) -> str:
        """The credential to present, resolved at the moment it is used.

        Deliberately not cached.  A ``kiln signin`` session token expires in
        about an hour and the bridge is a daemon that runs for days, so a
        bearer captured at construction would be dead by the first reconnect
        and the client would retry forever with a credential the relay can
        only refuse.  Re-resolving lets
        :func:`kiln.auth_session.resolve_api_bearer` hand back a refreshed
        token, which is the whole reason that resolver exists.  A license
        key passed in explicitly is honoured as-is and never re-read.
        """
        return self._pinned_license or _read_license()

    def _auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._bearer()}",
            "X-Kiln-Device-Fingerprint": _device_fingerprint(),
            "X-Kiln-Client-Version": _running_version(),
        }

    def _dispatch_frame(self, ws, req: Any) -> asyncio.Task | None:
        """Route one inbound frame: the relay's observe request is answered
        off to the side; everything else is a tool call.  Each runs as its
        own task so a slow address family or a slow slice never holds the
        socket's receive loop."""
        if not isinstance(req, dict):
            return None
        if "observe_nonce" in req:
            return asyncio.create_task(self._show_addresses(str(req["observe_nonce"])))
        return asyncio.create_task(self._handle_and_reply(ws, req))

    async def _show_addresses(self, nonce: str) -> None:
        """Show the relay this machine from each address family it has.

        Why: the relay tells "printing from the web at home" from "from
        miles away" by whether the browser arrives from the same public
        address as this machine — and a home usually has an IPv4 and an
        IPv6 side, while the socket shows only the one it happened to
        dial out over.  So the relay hands the bridge a nonce, and the
        bridge makes one small request per family carrying it; the relay
        records where each request CAME FROM.  Nothing is claimed in the
        body — an address a client could name is an address a client
        could forge — and a family this machine cannot reach is simply not
        shown.  Best-effort throughout; the print path never waits on it.
        """
        api = os.environ.get("KILN_API_URL", _DEFAULT_API_URL).rstrip("/")
        try:
            await asyncio.to_thread(observe_addresses, api, self._bearer(), nonce)
        except Exception:  # noqa: BLE001 — a missed observation is a coarser answer, never a fault
            logger.debug("bridge: address observation failed", exc_info=True)

    async def _handle_and_reply(self, ws, req: dict) -> None:
        resp = await asyncio.to_thread(
            handle_relay_request,
            req,
            call_tool=self._call_tool,
            fetch_artifact=self._fetch_artifact,
        )
        with contextlib.suppress(Exception):
            # socket gone; the server times that call out
            await ws.send(json.dumps(resp))

    async def run(self) -> None:
        if not self._bearer():
            raise RuntimeError(
                "Not signed in, so the relay can't route to this machine. "
                "Run 'kiln signin' (or 'kiln pair'), then enable web control."
            )
        import websockets  # local import: only needed when actually running

        backoff = 1.0
        write_bridge_state(connected=False)  # advertise "running"; flips true on connect
        while not self._stop:
            try:
                async with websockets.connect(
                    self._url, additional_headers=self._auth_headers()
                ) as ws:
                    logger.info("bridge connected to relay")
                    write_bridge_state(connected=True)
                    backoff = 1.0
                    async for raw in ws:
                        try:
                            req = json.loads(raw)
                        except Exception:
                            continue  # ignore a malformed frame, keep the link
                        self._dispatch_frame(ws, req)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                write_bridge_state(connected=False)
                logger.info("bridge link down (%s); retrying in %.0fs", exc, backoff)
                # A handshake refusal is the relay refusing our CREDENTIAL,
                # and one credential state is unrecoverable from this loop:
                # a session whose refresh token has been rejected.  Left
                # unsaid, that produced a measured 281-rejection retry storm
                # whose every line read "HTTP 403" and none read "run kiln
                # signin" — the one command that fixes it.  Asked once per
                # failure, said only when the resolver is certain.
                if "403" in str(exc):
                    try:
                        from kiln.auth_session import resolve_session_bearer

                        session = resolve_session_bearer()
                        if session.state == "needs_signin":
                            logger.warning(
                                "bridge: your Kiln session has expired and "
                                "can't refresh itself. Run `kiln signin` on "
                                "this machine, and the bridge will reconnect "
                                "on its own."
                            )
                    except Exception:  # diagnosis must never break the loop
                        logger.debug("session-state check failed", exc_info=True)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    def stop(self) -> None:
        self._stop = True


def _state_path() -> str:
    return os.path.expanduser(_STATE_FILE)


def read_bridge_state() -> dict[str, Any]:
    """Return the running bridge's last-written liveness state, or ``{}``.

    Consumed by ``kiln bridge status``; never raises on a missing or corrupt
    file (a bridge that isn't running simply has no state).
    """
    try:
        with open(_state_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_bridge_state(*, connected: bool) -> None:
    """Advertise THIS process as the running bridge (best-effort, never raises).

    ``since`` is preserved across a reconnect flap so status can honestly say
    "connected for 2h" rather than resetting on every dropped frame.

    ``version`` is the version this process is RUNNING, and it is the only
    place that fact is recorded on the machine.  A daemon started by launchd
    six weeks ago holds the code it imported then; every command typed since
    reports what is on disk now.  Writing it here is what lets
    ``kiln bridge status`` notice the two have parted company (see
    :mod:`kiln.bridge_version`).  Written on connect and on drop — never per
    relayed call, so nothing about a print touches this file.
    """
    try:
        prev = read_bridge_state()
        now = time.time()
        keep_since = connected and bool(prev.get("connected")) and prev.get("since")
        since = prev.get("since") if keep_since else (now if connected else None)
        path = _state_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(
                {"pid": os.getpid(), "connected": bool(connected),
                 "since": since, "updated": now,
                 "version": _running_version()},
                fh,
            )
        os.replace(tmp, path)
    except OSError:
        pass


def clear_bridge_state() -> None:
    """Remove the liveness file on shutdown (best-effort)."""
    with contextlib.suppress(OSError):
        os.unlink(_state_path())


# ---------------------------------------------------------------------------
# The account's answer, read by the home box
# ---------------------------------------------------------------------------
#
# Interface contracts only.  When this machine is signed in (``kiln signin``),
# the Kiln API may hold a person's answer about a print this machine asked
# about: the routes under ``/api/print-authority/`` are served by kiln-pro
# (https://kiln3d.com/pricing).  This section sends the fields named below
# and reads the fields named below — nothing else.  Whether an answer is
# given, and what it covers, is the server's to say.  Every call is short and
# never raises; "not signed in" and "the server did not answer" are both no
# answer, logged at debug, and the caller goes on down its own ladder.

_ACCOUNT_ROUTE = "/api/print-authority"
#: ``(connect, read)`` seconds.  A print start waits on these, so they are
#: short: a slow answer is no answer, and the ladder goes on without it.
_ACCOUNT_TIMEOUT_S = (3.0, 5.0)
#: The largest picture an ask may carry (the route's own limit, 200 KB).
PICTURE_MAX_BYTES = 200 * 1024
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
#: How long this process trusts its own memo of a live ask when the
#: server's answer carried no readable end.
_ASK_MEMO_FALLBACK_S = 60.0


@dataclass(frozen=True)
class AccountAsk:
    """What ``POST /api/print-authority/pending`` said about one ask."""

    id: str = ""
    expires_at: float | None = None
    #: The server already held a live ask for these bytes on this printer.
    repeat: bool = False
    #: Whether a picture rode the post.
    picture_sent: bool = False
    #: ``False`` when this process answered from its own memo of a live
    #: ask and posted nothing.
    posted: bool = True
    #: The server's refusal code when it would not hold the ask
    #: (``""`` when it did).
    refused: str = ""


@dataclass(frozen=True)
class AccountAnswer:
    """``GET /api/print-authority/may-i-print``, in the fields read here."""

    allowed: bool
    #: ``authority.kind`` — ``"approval"`` or ``"machine_window"`` when allowed.
    kind: str = ""
    id: str = ""
    grantor: str = ""
    expires_at: float | None = None
    via: str = ""
    #: ``pending.id`` / ``pending.state`` — the newest ask for these bytes
    #: on this printer, when the server has one.
    pending_id: str = ""
    pending_state: str = ""


_ask_lock = threading.Lock()
#: ``{(file_sha256, printer): AccountAsk}`` — the live asks this process
#: posted, so a start retried every few seconds does not post again.
_asks: dict[tuple[str, str], AccountAsk] = {}


def _ask_key(file_sha256: str, printer_name: str) -> tuple[str, str]:
    return str(file_sha256 or "").strip().lower(), str(printer_name or "").strip().lower()


def live_ask(file_sha256: str, printer_name: str) -> AccountAsk | None:
    """The ask this process posted for these bytes on this printer, while
    it is live."""
    key = _ask_key(file_sha256, printer_name)
    with _ask_lock:
        ask = _asks.get(key)
        if ask is not None and ask.expires_at is not None and ask.expires_at <= time.time():
            _asks.pop(key, None)
            return None
        return ask


def forget_ask(file_sha256: str, printer_name: str) -> None:
    """Drop this process's memo of an ask — it was answered, or it ran out."""
    with _ask_lock:
        _asks.pop(_ask_key(file_sha256, printer_name), None)


def holds_an_ask(printer_name: str) -> bool:
    """Whether this process holds a live ask for any file on this printer —
    the cheap check before hashing a file to find which."""
    printer = _ask_key("", printer_name)[1]
    now = time.time()
    with _ask_lock:
        return any(
            key[1] == printer and (ask.expires_at is None or ask.expires_at > now)
            for key, ask in _asks.items()
        )


def _reset_asks_for_tests() -> None:
    with _ask_lock:
        _asks.clear()


def _api_base() -> str:
    return (os.environ.get("KILN_API_URL") or _DEFAULT_API_URL).rstrip("/")


def account_bearer() -> str:
    """The signed-in person's session bearer (``kiln signin`` / ``kiln
    pair``), or ``""``.  The session, not a license key: the ask and the
    answer are a person's.  Never raises."""
    try:
        from kiln.auth_session import resolve_session_bearer

        return resolve_session_bearer().token.strip()
    except Exception:  # noqa: BLE001 — auth trouble is no bearer
        logger.debug("account: session bearer unresolved", exc_info=True)
        return ""


def account_signed_out() -> bool:
    """Whether this machine has no sign-in the account door could use — so
    ``kiln signin`` is the thing to suggest.  No network: a session that can
    renew itself counts as signed in (whether it renews is the next call's
    to find out), and any other session is judged by
    :func:`kiln.auth_session.resolve_session_bearer`, which touches the
    network only to renew."""
    try:
        from kiln.auth_session import _read_tokens, resolve_session_bearer

        stored = _read_tokens()
        if str(stored.get("access_token") or "").strip() and str(stored.get("refresh_token") or "").strip():
            return False
        return not resolve_session_bearer().token.strip()
    except Exception:  # noqa: BLE001 — unreadable auth is no sign-in
        return True


def _epoch(value: Any) -> float | None:
    """A time the server sent — epoch seconds or ISO 8601 — as epoch."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip()
    if not text:
        return None
    with contextlib.suppress(ValueError):
        return float(text)
    try:
        from datetime import datetime, timezone

        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _account_call(method: str, path: str, bearer: str, **kwargs: Any) -> tuple[int, dict[str, Any]] | None:
    """One request to the account routes: ``(status, body)``, or ``None``
    when the server could not be reached.  Never raises."""
    import requests

    try:
        resp = requests.request(
            method, f"{_api_base()}{_ACCOUNT_ROUTE}{path}",
            headers={"Authorization": f"Bearer {bearer}"}, timeout=_ACCOUNT_TIMEOUT_S, **kwargs,
        )
    except Exception as exc:  # noqa: BLE001 — offline, DNS, TLS, a timeout
        logger.debug("account: %s %s unreachable: %s", method, path, type(exc).__name__)
        return None
    try:
        body = resp.json()
    except ValueError:
        body = {}
    return resp.status_code, body if isinstance(body, dict) else {}


def picture_for_ask(path: str | None) -> bytes | None:
    """The still at *path* as PNG bytes no larger than
    :data:`PICTURE_MAX_BYTES` — as it is when it fits, else shrunk with
    Pillow — or ``None``.  The ask binds the person's yes to these exact
    bytes, so whatever is returned here is what is hashed and sent."""
    if not path:
        return None
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError:
        return None
    if raw[:8] == _PNG_MAGIC and len(raw) <= PICTURE_MAX_BYTES:
        return raw
    try:
        import io

        from PIL import Image

        with Image.open(io.BytesIO(raw)) as src:
            image = src.convert("RGBA") if src.mode not in ("RGB", "RGBA") else src.copy()
        for side in (800, 640, 480, 360, 240):
            smaller = image.copy()
            smaller.thumbnail((side, side))
            buf = io.BytesIO()
            smaller.save(buf, format="PNG", optimize=True)
            data = buf.getvalue()
            if len(data) <= PICTURE_MAX_BYTES:
                return data
    except Exception:  # noqa: BLE001 — a picture that cannot be read is no picture
        logger.debug("account: picture %s not usable", path, exc_info=True)
    return None


def _observe_in_background(api: str, bearer: str, nonce: str) -> None:
    """:func:`observe_addresses` off to the side: the print start that
    posted the ask never waits on it."""
    threading.Thread(
        target=observe_addresses, args=(api, bearer, nonce), name="kiln-ask-observe", daemon=True,
    ).start()


def ask_the_account(
    *, file_sha256: str, file_name: str, printer_name: str, picture_path: str | None, shown_door: str,
) -> AccountAsk | None:
    """``POST /api/print-authority/pending``: ask the signed-in account
    about this print, once while an ask is live.

    Sends ``file_sha256``, ``file_name``, ``printer_name``,
    ``shown_pixels_sha`` (the sha256 of the picture bytes sent, or ``""``),
    ``shown_door``, ``picture_png_b64`` (``""`` when there is none),
    ``machine_fingerprint`` (this install's heartbeat device) and
    ``observe_nonce``; reads ``pending.id``, ``pending.expires_at`` and
    ``pending.repeat``.  After a held ask the machine is shown to the relay
    with the nonce (:func:`observe_addresses`).

    ``None`` when there is nobody to ask for or the server did not answer;
    an :class:`AccountAsk` with ``refused`` set when it answered with a
    refusal.  Never raises.
    """
    if not file_sha256 or not printer_name:
        return None
    memo = live_ask(file_sha256, printer_name)
    if memo is not None:
        return AccountAsk(
            id=memo.id, expires_at=memo.expires_at, repeat=True, picture_sent=memo.picture_sent, posted=False,
        )
    bearer = account_bearer()
    if not bearer:
        return None
    try:
        from kiln.device import get_device_fingerprint

        machine = str(get_device_fingerprint() or "").strip()
    except Exception:  # noqa: BLE001
        machine = ""
    if not machine:
        logger.debug("account: no machine fingerprint on this install; nothing asked")
        return None
    import base64
    import hashlib
    import secrets

    picture = picture_for_ask(picture_path)
    nonce = secrets.token_urlsafe(18)
    body = {
        "file_sha256": str(file_sha256).lower(),
        "file_name": os.path.basename(str(file_name or "")),
        "printer_name": printer_name,
        "shown_pixels_sha": hashlib.sha256(picture).hexdigest() if picture else "",
        "shown_door": shown_door or "",
        "picture_png_b64": base64.b64encode(picture).decode("ascii") if picture else "",
        "machine_fingerprint": machine,
        "observe_nonce": nonce,
    }
    answered = _account_call("POST", "/pending", bearer, json=body)
    if answered is None:
        return None
    status, data = answered
    held = data.get("pending") if isinstance(data.get("pending"), dict) else {}
    if not (200 <= status < 300) or not str(held.get("id") or ""):
        code = str(data.get("error") or f"http_{status}")
        logger.debug("account: ask not held (%s)", code)
        return AccountAsk(picture_sent=picture is not None, refused=code)
    expires_at = _epoch(held.get("expires_at"))
    ask = AccountAsk(
        id=str(held["id"]),
        expires_at=expires_at if expires_at is not None else time.time() + _ASK_MEMO_FALLBACK_S,
        repeat=bool(held.get("repeat")),
        picture_sent=picture is not None,
    )
    with _ask_lock:
        _asks[_ask_key(file_sha256, printer_name)] = ask
    with contextlib.suppress(Exception):
        _observe_in_background(_api_base(), bearer, nonce)
    return ask


def withdraw_ask(file_sha256: str, printer_name: str) -> tuple[str, bool] | None:
    """``POST /api/print-authority/pending/{id}/withdraw`` for the ask this
    process posted about these bytes on this printer: another door
    answered the print.  ``(id, withdrawn)``, or ``None`` when this process
    holds no such ask.  The memo is dropped either way — an ask the server
    would not withdraw runs out on its own.  Never raises."""
    ask = live_ask(file_sha256, printer_name)
    if ask is None:
        return None
    forget_ask(file_sha256, printer_name)
    bearer = account_bearer()
    if not bearer:
        return ask.id, False
    from urllib.parse import quote

    answered = _account_call("POST", f"/pending/{quote(ask.id, safe='')}/withdraw", bearer, json={})
    if answered is None:
        return ask.id, False
    status, data = answered
    if not (200 <= status < 300):
        logger.debug("account: withdraw answered %s (%s)", status, data.get("error"))
        return ask.id, False
    return ask.id, True


def read_the_account(*, file_sha256: str, printer_name: str) -> AccountAnswer | None:
    """``GET /api/print-authority/may-i-print?printer_name=&file_hash=``.

    Reads ``allowed``; ``authority.kind``, ``.id``, ``.grantor``,
    ``.expires_at`` and ``.via``; and ``pending.id`` / ``pending.state``.
    ``None`` when not signed in or the server did not answer.  Never
    raises."""
    if not file_sha256 or not printer_name:
        return None
    bearer = account_bearer()
    if not bearer:
        return None
    answered = _account_call(
        "GET", "/may-i-print", bearer,
        params={"printer_name": printer_name, "file_hash": str(file_sha256).lower()},
    )
    if answered is None:
        return None
    status, data = answered
    if not (200 <= status < 300):
        logger.debug("account: may-i-print answered %s (%s)", status, data.get("error"))
        return None
    authority = data.get("authority") if isinstance(data.get("authority"), dict) else {}
    pending = data.get("pending") if isinstance(data.get("pending"), dict) else {}
    return AccountAnswer(
        allowed=data.get("allowed") is True,
        kind=str(authority.get("kind") or ""),
        id=str(authority.get("id") or ""),
        grantor=str(authority.get("grantor") or ""),
        expires_at=_epoch(authority.get("expires_at")),
        via=str(authority.get("via") or ""),
        pending_id=str(pending.get("id") or ""),
        pending_state=str(pending.get("state") or ""),
    )


def record_start(*, authority_id: str, kind: str, file_sha256: str, printer_name: str) -> bool:
    """``POST /api/print-authority/record-start`` with ``authority_id``,
    ``kind``, ``file_sha256`` and ``printer_name``: this machine is starting
    the print that answer covers.  True only when the server said so (a 2xx
    carrying ``event``).  Never raises."""
    if not authority_id:
        return False
    bearer = account_bearer()
    if not bearer:
        return False
    answered = _account_call(
        "POST", "/record-start", bearer,
        json={
            "authority_id": authority_id, "kind": kind,
            "file_sha256": str(file_sha256).lower(), "printer_name": printer_name,
        },
    )
    if answered is None:
        return False
    status, data = answered
    if not (200 <= status < 300) or not isinstance(data.get("event"), dict):
        logger.debug("account: record-start answered %s (%s)", status, data.get("error"))
        return False
    return True


def run_bridge() -> None:
    """Blocking entry point: ``python -m kiln.bridge_client``."""
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(BridgeClient().run())
    finally:
        clear_bridge_state()


if __name__ == "__main__":
    run_bridge()
