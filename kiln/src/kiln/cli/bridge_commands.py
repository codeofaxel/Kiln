"""``kiln bridge`` — let kiln3d.com print to THIS machine's printers.

The bridge is the outbound relay client (:mod:`kiln.bridge_client`): it dials
OUT to Kiln's relay and runs web-issued, relay-safe tool calls locally against
your own printers — the same tools the MCP path already runs.  Outbound-only,
so it opens no inbound port and exposes nothing on your network.

It runs ONLY when you turn it on here.  Installing Kiln never connects anything;
``enable`` (or ``start``) is the one conscious opt-in.

    kiln bridge enable     on for good — start now and on every login
    kiln bridge disable    off — stop now and stop starting on login
    kiln bridge start      run once in the background (until you log out)
    kiln bridge stop       stop the background run
    kiln bridge status     is it on, and is it connected?

Needs a signed-in account (``kiln signin`` / ``kiln pair``): the relay routes a
call only to the bridge running on the SAME account.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

import click

from kiln.bridge_client import (
    _read_license,
    clear_bridge_state,
    read_bridge_state,
)

# launchd label (macOS) and systemd unit stem (Linux).  One name so status,
# install, and remove all agree on what "the service" is.
_LABEL = "com.kiln3d.bridge"
_UNIT = "kiln-bridge.service"
_LOG_FILE = os.path.expanduser("~/.kiln/bridge.log")


# ---------------------------------------------------------------------------
# Pure helpers (tested without a printer, a socket, or launchd)
# ---------------------------------------------------------------------------


def _ago(seconds: float) -> str:
    """Human-friendly elapsed time: ``2h 14m`` / ``5m`` / ``just now``."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return "just now"
    m = seconds // 60
    if m < 60:
        return f"{m}m"
    return f"{m // 60}h {m % 60}m"


def _render_plist(python: str, log_path: str) -> str:
    """macOS LaunchAgent: run the bridge at login and keep it alive."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        "<dict>\n"
        f"    <key>Label</key><string>{_LABEL}</string>\n"
        "    <key>ProgramArguments</key>\n"
        "    <array>\n"
        f"        <string>{python}</string>\n"
        "        <string>-m</string>\n"
        "        <string>kiln.bridge_client</string>\n"
        "    </array>\n"
        "    <key>RunAtLoad</key><true/>\n"
        "    <key>KeepAlive</key><true/>\n"
        "    <key>ProcessType</key><string>Background</string>\n"
        f"    <key>StandardOutPath</key><string>{log_path}</string>\n"
        f"    <key>StandardErrorPath</key><string>{log_path}</string>\n"
        "</dict>\n"
        "</plist>\n"
    )


def _render_systemd_unit(python: str) -> str:
    """Linux systemd --user unit: run the bridge at login, restart on crash."""
    return (
        "[Unit]\n"
        "Description=Kiln bridge (web-to-printer relay client)\n"
        "After=network-online.target\n\n"
        "[Service]\n"
        f"ExecStart={python} -m kiln.bridge_client\n"
        "Restart=always\n"
        "RestartSec=3\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _describe_status(
    *,
    signed_in: bool,
    enabled: bool,
    running: bool,
    connected: bool,
    since: float | None,
    now: float,
) -> tuple[str, list[str]]:
    """Map the facts to a (headline, detail-lines) pair — honest in every state.

    The ONLY place the state machine lives, so status never lies and the tests
    can walk the whole matrix.
    """
    if not signed_in:
        return "signed out", [
            "Sign in first so the relay can find your bridge:",
            "  kiln signin   (or  kiln pair)",
        ]

    lines: list[str] = []
    if connected:
        headline = "on, connected"
        if since:
            lines.append(f"Connected to the relay for {_ago(now - since)}.")
        lines.append("Prints from kiln3d.com reach this machine's printers.")
    elif running:
        headline = "on, connecting…"
        lines += [
            "Running, but not connected to the relay yet.",
            f"If this persists, check the log: {_LOG_FILE}",
        ]
    elif enabled:
        headline = "enabled, but not running"
        lines += [
            "Set to start on login, but not running right now.",
            f"Start it now: kiln bridge start   ·   Log: {_LOG_FILE}",
        ]
    else:
        return "off", [
            "Turn it on for good:  kiln bridge enable   (starts now + every login)",
            "Just this session:    kiln bridge start",
        ]

    if enabled:
        lines.append("Starts automatically on every login.")
    else:
        lines.append("Make it automatic: kiln bridge enable")
    return headline, lines


# ---------------------------------------------------------------------------
# Process supervision (manual `start` / `stop`)
# ---------------------------------------------------------------------------


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else — still "alive"
    except OSError:
        return False
    return True


def _running_pid() -> int | None:
    """The live bridge pid from the state file, or ``None`` if not running."""
    pid = read_bridge_state().get("pid")
    if isinstance(pid, int) and _pid_alive(pid):
        return pid
    return None


def _spawn_bridge() -> int:
    """Launch the bridge detached from this terminal; return its pid."""
    os.makedirs(os.path.dirname(_LOG_FILE), exist_ok=True)
    log = open(_LOG_FILE, "a", encoding="utf-8")  # noqa: SIM115 — handed to the child
    kwargs: dict = {"stdout": log, "stderr": log, "stdin": subprocess.DEVNULL}
    if os.name == "posix":
        kwargs["start_new_session"] = True  # leave the controlling terminal
    else:  # Windows: DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        kwargs["creationflags"] = 0x00000008 | 0x00000200
    proc = subprocess.Popen(
        [sys.executable, "-m", "kiln.bridge_client"], **kwargs
    )
    return proc.pid


def _stop_process() -> bool:
    """SIGTERM the running bridge and wait briefly for it to exit."""
    pid = _running_pid()
    if pid is None:
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass
    for _ in range(30):  # up to ~3s
        if not _pid_alive(pid):
            break
        time.sleep(0.1)
    clear_bridge_state()
    return not _pid_alive(pid)


def _await_connected(timeout: float = 6.0) -> bool:
    """Poll the liveness file until the bridge reports connected (bounded).

    Lets ``enable`` / ``start`` confirm the outcome instead of leaving the user
    to guess whether it worked.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if read_bridge_state().get("connected") and _running_pid() is not None:
            return True
        time.sleep(0.25)
    return False


# ---------------------------------------------------------------------------
# Login service (enable / disable) — launchd on macOS, systemd --user on Linux
# ---------------------------------------------------------------------------


def _plist_path() -> str:
    return os.path.expanduser(f"~/Library/LaunchAgents/{_LABEL}.plist")


def _systemd_path() -> str:
    return os.path.expanduser(f"~/.config/systemd/user/{_UNIT}")


def _service_installed() -> bool:
    if sys.platform == "darwin":
        return os.path.exists(_plist_path())
    if sys.platform.startswith("linux"):
        return os.path.exists(_systemd_path())
    return False


def _atomic_write(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(content)
    os.replace(tmp, path)


def _install_service() -> tuple[bool, str]:
    """Write + load the login service.  Returns (ok, error_detail)."""
    if sys.platform == "darwin":
        path = _plist_path()
        _atomic_write(path, _render_plist(sys.executable, _LOG_FILE))
        subprocess.run(["launchctl", "unload", path], capture_output=True, text=True)
        r = subprocess.run(
            ["launchctl", "load", "-w", path], capture_output=True, text=True
        )
        if r.returncode != 0:
            return False, (r.stderr or "").strip() or "launchctl load failed"
        return True, ""
    if sys.platform.startswith("linux"):
        _atomic_write(_systemd_path(), _render_systemd_unit(sys.executable))
        subprocess.run(
            ["systemctl", "--user", "daemon-reload"], capture_output=True, text=True
        )
        r = subprocess.run(
            ["systemctl", "--user", "enable", "--now", _UNIT],
            capture_output=True,
            text=True,
        )
        if r.returncode != 0:
            return False, (r.stderr or "").strip() or "systemctl enable failed"
        return True, ""
    return False, "unsupported-platform"


def _remove_service() -> None:
    """Stop + remove the login service (idempotent, best-effort)."""
    if sys.platform == "darwin":
        path = _plist_path()
        if os.path.exists(path):
            subprocess.run(["launchctl", "unload", "-w", path], capture_output=True, text=True)
            try:
                os.unlink(path)
            except OSError:
                pass
    elif sys.platform.startswith("linux") and os.path.exists(_systemd_path()):
        subprocess.run(
            ["systemctl", "--user", "disable", "--now", _UNIT],
            capture_output=True,
            text=True,
        )
        try:
            os.unlink(_systemd_path())
        except OSError:
            pass
        subprocess.run(
            ["systemctl", "--user", "daemon-reload"], capture_output=True, text=True
        )


def _preflight() -> None:
    """Fail-fast checks before we start dialing out."""
    if not _read_license():
        raise click.ClickException(
            "Sign in first so the relay can route to you:\n"
            "  kiln signin   (or  kiln pair)"
        )
    try:
        import websockets  # noqa: F401,PLC0415
    except ImportError:
        raise click.ClickException(
            "The 'websockets' package isn't installed, so the bridge can't "
            "connect.\n  pip install websockets"
        ) from None


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@click.group()
def bridge() -> None:
    """Let kiln3d.com print to this machine's printers (opt-in, outbound-only)."""


@bridge.command()
def status() -> None:
    """Show whether the bridge is on and connected."""
    st = read_bridge_state()
    running = _running_pid() is not None
    connected = bool(st.get("connected")) and running
    headline, lines = _describe_status(
        signed_in=bool(_read_license()),
        enabled=_service_installed(),
        running=running,
        connected=connected,
        since=st.get("since"),
        now=time.time(),
    )
    # Default terminal colour for the off/idle states — a fixed "white" is
    # invisible on a light-background terminal.
    dot = "green" if connected else ("yellow" if running else None)
    styled = click.style(headline, fg=dot) if dot else headline
    click.echo(click.style("Bridge: ", bold=True) + styled)
    for line in lines:
        click.echo(f"  {line}")


@bridge.command()
def enable() -> None:
    """Turn the bridge on for good — start now and on every login."""
    _preflight()
    _stop_process()  # fold any manual run into the managed one — never two bridges
    ok, detail = _install_service()
    if not ok:
        if detail == "unsupported-platform":
            raise click.ClickException(
                "Starting on login isn't supported on this platform yet.\n"
                "Run it for this session instead: kiln bridge start"
            )
        raise click.ClickException(f"Couldn't enable the login service: {detail}")
    click.echo(
        click.style("Bridge on.", fg="green")
        + " It starts automatically every time you log in."
    )
    if _await_connected():
        click.echo("  Connected ✓ — prints from kiln3d.com now reach this machine's printers.")
    else:
        click.echo("  Connecting… confirm with: kiln bridge status")
    click.echo("  Turn it off any time: kiln bridge disable")


@bridge.command()
def disable() -> None:
    """Turn the bridge off — stop now and stop starting on login."""
    was = _service_installed() or _running_pid() is not None
    _remove_service()
    _stop_process()
    clear_bridge_state()
    if was:
        click.echo(
            click.style("Bridge off.", fg="yellow")
            + " It won't run or reconnect until you enable it again."
        )
    else:
        click.echo("Bridge was already off.")


@bridge.command()
def start() -> None:
    """Run the bridge once, in the background (until you log out)."""
    if _service_installed():
        click.echo("Already set to start on login — it's managed for you.")
        click.echo("  See it: kiln bridge status   ·   Turn off: kiln bridge disable")
        return
    if _running_pid() is not None:
        click.echo("Bridge is already running.  See: kiln bridge status")
        return
    _preflight()
    pid = _spawn_bridge()
    if _await_connected():
        click.echo(click.style("Bridge on ✓", fg="green") + f" (pid {pid}) — connected.")
        click.echo("  Prints from kiln3d.com now reach this machine's printers.")
    else:
        click.echo(click.style("Bridge started", fg="green") + f" (pid {pid}) — connecting…")
        click.echo("  Confirm with: kiln bridge status")
    click.echo("  Stop it: kiln bridge stop   ·   Start on every login: kiln bridge enable")


@bridge.command()
def stop() -> None:
    """Stop the background bridge run."""
    if _service_installed():
        click.echo("The bridge is set to start on login, so it restarts if stopped.")
        click.echo("  To turn it off, run: kiln bridge disable")
        return
    if _stop_process():
        click.echo(click.style("Bridge stopped.", fg="yellow"))
    else:
        click.echo("Bridge isn't running.")


def register_bridge_cli(cli_group: click.Group) -> None:
    """Attach ``kiln bridge {status,start,stop,enable,disable}``.

    Called unconditionally from ``kiln.cli.main`` — the bridge ships in the
    public package and depends on nothing proprietary.
    """
    cli_group.add_command(bridge)
