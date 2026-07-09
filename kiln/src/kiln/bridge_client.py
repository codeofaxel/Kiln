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
import json
import logging
import os
import tempfile
import time
import urllib.request
from typing import Any, Callable

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
    """
    request_id = req.get("request_id")
    tool = str(req.get("tool_name") or "")
    args = dict(req.get("args") or {})
    try:
        token = args.pop("cloud_artifact_token", None)
        if tool == "slice_and_print" and token:
            args["input_path"] = fetch_artifact(str(token))
        result = call_tool(tool, args)
        return {"request_id": request_id, "ok": True, "result": result}
    except Exception as exc:  # deliberately broad — one call must not kill the ws
        logger.info("relay tool %r failed: %s", tool, exc)
        return {
            "request_id": request_id,
            "ok": False,
            "error": {"message": str(exc), "tool": tool},
        }


# ---------------------------------------------------------------------------
# Default (production) dependencies
# ---------------------------------------------------------------------------


def _default_tool_caller() -> ToolCaller:
    """Invoke a registered Kiln tool by name — the same functions the MCP runs.

    Reuses the server's tool registry so every safety gate the tool already
    carries (preflight, auto-print-off default, validation) applies unchanged.
    """
    from kiln import server as _server

    def call_tool(name: str, args: dict) -> Any:
        tool = _server.mcp._tool_manager._tools.get(name)
        if tool is None:
            raise ValueError(f"tool {name!r} is not available on this machine")
        return tool.fn(**args)

    return call_tool


def _default_artifact_fetcher(license_key: str) -> ArtifactFetcher:
    """Fetch a saved make's geometry from the cloud to a local temp file.

    The web only holds a cloud reference; the bridge pulls the actual mesh with
    the user's license so it has a real local path for ``slice_and_print``.
    """
    api = os.environ.get("KILN_API_URL", _DEFAULT_API_URL).rstrip("/")

    def fetch(token: str) -> str:
        url = f"{api}/api/artifact/{token}"
        request = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {license_key}"}
        )
        with urllib.request.urlopen(request, timeout=30) as resp:  # noqa: S310
            data = resp.read()
        fd, path = tempfile.mkstemp(suffix=".stl", prefix="kiln-relay-")
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        return path

    return fetch


def _read_license() -> str:
    """Best-effort read of the user's license key (env, then ~/.kiln/config)."""
    key = os.environ.get("KILN_LICENSE_KEY", "").strip()
    if key:
        return key
    try:
        import yaml  # kiln already depends on PyYAML

        cfg_path = os.path.expanduser("~/.kiln/config.yaml")
        with open(cfg_path, encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
        return str(cfg.get("license_key") or "").strip()
    except Exception:
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
        self._license = license_key or _read_license()
        self._url = relay_url or os.environ.get("KILN_RELAY_URL", _DEFAULT_RELAY_URL)
        self._call_tool = call_tool or _default_tool_caller()
        self._fetch_artifact = fetch_artifact or _default_artifact_fetcher(self._license)
        self._stop = False

    def _auth_headers(self) -> dict[str, str]:
        from kiln import __version__ as _v  # noqa: PLC0415

        return {
            "Authorization": f"Bearer {self._license}",
            "X-Kiln-Device-Fingerprint": _device_fingerprint(),
            "X-Kiln-Client-Version": str(_v),
        }

    async def _handle_and_reply(self, ws, req: dict) -> None:
        resp = await asyncio.to_thread(
            handle_relay_request,
            req,
            call_tool=self._call_tool,
            fetch_artifact=self._fetch_artifact,
        )
        try:
            await ws.send(json.dumps(resp))
        except Exception:
            pass  # socket gone; the server times that call out

    async def run(self) -> None:
        if not self._license:
            raise RuntimeError(
                "No license key found. Set KILN_LICENSE_KEY or add license_key to "
                "~/.kiln/config.yaml, then enable web control."
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
                        asyncio.create_task(self._handle_and_reply(ws, req))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                write_bridge_state(connected=False)
                logger.info("bridge link down (%s); retrying in %.0fs", exc, backoff)
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
                 "since": since, "updated": now},
                fh,
            )
        os.replace(tmp, path)
    except OSError:
        pass


def clear_bridge_state() -> None:
    """Remove the liveness file on shutdown (best-effort)."""
    try:
        os.unlink(_state_path())
    except OSError:
        pass


def run_bridge() -> None:
    """Blocking entry point: ``python -m kiln.bridge_client``."""
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(BridgeClient().run())
    finally:
        clear_bridge_state()


if __name__ == "__main__":
    run_bridge()
