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
    kiln bridge restart    cycle it, however it happens to be supervised
    kiln bridge status     is it on, is it connected, is it current?

Start-on-login is per-user and needs no elevation on all three platforms:
launchd LaunchAgent (macOS), systemd --user unit (Linux), HKCU Run key
(Windows, windowless via pythonw).

Either way the bridge is watched, and by exactly one thing.  ``enable`` hands
the job to launchd (``KeepAlive``) or systemd (``Restart=always``) where those
exist, since they also survive a reboot.  ``start`` — and ``enable`` on
Windows, whose Run key launches and forgets — runs
:mod:`kiln.bridge_supervisor` instead, which restarts a crashed bridge for as
long as the session lasts.  ``start`` never installs a login item: it offers
``enable`` and leaves that choice to the person typing.

Needs a signed-in account (``kiln signin`` / ``kiln pair``): the relay routes a
call only to the bridge running on the SAME account.
"""
from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
import urllib.parse
from collections.abc import Sequence
from typing import NamedTuple

import click

from kiln.bridge_client import (
    _read_license,
    clear_bridge_state,
    read_bridge_state,
)
from kiln.bridge_supervisor import (
    clear_supervisor_state,
    read_supervisor_state,
    supervisor_pid,
)
from kiln.bridge_version import RESTART_COMMAND
from kiln.bridge_version import describe as describe_bridge_version

# launchd label (macOS), systemd unit stem (Linux), and registry Run value
# (Windows).  One name each so status, install, and remove all agree on what
# "the service" is.
_LABEL = "com.kiln3d.bridge"
_UNIT = "kiln-bridge.service"
_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_RUN_VALUE = "KilnBridge"
_LOG_FILE = os.path.expanduser("~/.kiln/bridge.log")

# Windows-only: the `kiln://` URI scheme, registered under HKCU (per-user, no
# elevation) so kiln3d.com can offer a real one-click "Connect my printer"
# link instead of a copy-paste command — but only ever AFTER the first
# `kiln bridge enable`, which is what registers it. macOS/Linux have no
# equivalent yet: a pip package cannot register a URL scheme on macOS at all
# (that needs a signed .app's Info.plist), and Linux's xdg-mime path is real
# but unbuilt, untested — see the standalone-decision note in tasks.md.
_PROTOCOL_SCHEME = "kiln"
_PROTOCOL_CLASS_KEY = rf"Software\Classes\{_PROTOCOL_SCHEME}"
_PROTOCOL_COMMAND_KEY = rf"{_PROTOCOL_CLASS_KEY}\shell\open\command"


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


def _render_run_command(python: str) -> str:
    """Windows Run-key command: quoted interpreter + the supervisor.

    The supervisor, unlike the plist and the systemd unit above, because the
    Run key is the one login mechanism of the three that does not watch what it
    started: launchd has ``KeepAlive`` and systemd has ``Restart=always``, and
    stacking our own supervisor under either would mean two parents fighting
    over one child.  Windows has no such parent, so it gets ours.
    """
    return f'"{python}" -m kiln.bridge_supervisor'


def _render_protocol_command(python: str) -> str:
    """Windows protocol-handler command: what runs when a `kiln://` link is
    clicked. ``%1`` is the OS's placeholder for the full clicked URI —
    Windows substitutes it, this code never parses argv itself for that part.
    """
    return f'"{python}" -m kiln.cli.main bridge handle-uri "%1"'


def _windows_pythonw() -> str:
    """Prefer ``pythonw.exe`` (no console window) for the login launch.

    The registry Run key starts console apps WITH a console window;
    ``pythonw.exe`` is the same interpreter built windowless.  Fall back to
    ``sys.executable`` when it isn't present (conda/embedded layouts).
    """
    candidate = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return candidate if os.path.exists(candidate) else sys.executable


class _Bearer(NamedTuple):
    """What we have to authenticate with, and why we don't have it."""

    token: str
    detail: str


def _resolve_bearer() -> _Bearer:
    """The bridge's credential, plus the one sentence explaining any absence.

    Two authorities, deliberately: :func:`_read_license` decides WHETHER we
    have a bearer — it alone knows the ``config.yaml`` license fallback that
    the session resolver has no idea about — and
    :func:`~kiln.auth_session.resolve_api_bearer` is asked only for WHY when
    there is nothing, since it can tell an expired session from a machine
    that never signed in.  Consulting the session resolver for the verdict
    instead would report "signed out" to an operator whose license key in
    config.yaml is working fine.
    """
    token = _read_license()
    if token:
        return _Bearer(token=token, detail="")
    try:
        from kiln.auth_session import resolve_api_bearer

        return _Bearer(token="", detail=resolve_api_bearer().detail)
    except Exception:
        return _Bearer(token="", detail="")


def _describe_status(
    *,
    signed_in: bool,
    enabled: bool,
    running: bool,
    connected: bool,
    since: float | None,
    now: float,
    signin_detail: str = "",
    supervised: bool = False,
    restarts: int = 0,
    last_exit_at: float | None = None,
    gave_up: bool = False,
    version_lines: Sequence[str] = (),
) -> tuple[str, list[str]]:
    """Map the facts to a (headline, detail-lines) pair — honest in every state.

    The ONLY place the state machine lives, so status never lies and the tests
    can walk the whole matrix.

    The crash facts (*restarts*, *last_exit_at*, *gave_up*) are here because a
    bridge that died is the one thing this command used to be unable to
    mention.  A crash-and-restart looked identical to a bridge that had been up
    all night, and a bridge that had given up looked identical to one that was
    simply never turned on — the same "off" and the same two suggestions, with
    no hint that something had tried and failed.  Both now say so.

    *version_lines* arrive already worded (:func:`kiln.bridge_version.describe`
    owns that); what belongs here is WHERE they go, which is a state-machine
    decision.  They are shown only while the bridge is actually up, because
    both things they can say are about a live process: a bridge that is off has
    nothing to restart, and the next start picks up whatever is installed by
    itself.  On a bridge that is off, the one fact worth acting on is that it
    is off — a version note underneath it would be a second errand competing
    with the only one that matters.
    """
    if not signed_in:
        # An expired session and a machine that never signed in both land
        # here, and they are not the same problem.  ``resolve_api_bearer``
        # already knows which — say it, rather than making the user guess
        # why "sign in" didn't seem to take the first time.
        lead = [signin_detail] if signin_detail else []
        return "signed out", [
            *lead,
            "Sign in so the relay can find your bridge:",
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
    elif gave_up:
        # The bridge kept dying seconds after each start and the supervisor
        # stopped trying.  Saying "off" here would be true and useless.
        #
        # Checked BEFORE `enabled`, because on Windows `enable` is supervised
        # by us and can reach this state — and "enabled, but not running" is
        # the wrong half of the story when we know exactly why it isn't.  On
        # macOS and Linux `enable` hands over to launchd/systemd, and enabling
        # clears any record a previous `start` left, so a stale give-up cannot
        # shadow a healthy login service.
        return "off after repeated crashes", [
            "The bridge kept stopping right after it started, so Kiln stopped "
            "restarting it.",
            f"What happened is in the log: {_LOG_FILE}",
            # Which verb works depends on `enabled`, and this is the one branch
            # that can be either.  Windows is why: `enable` there installs a Run
            # key AND runs our own supervisor, so a give-up is reachable with the
            # login service still in place — and in that state `kiln bridge
            # start` refuses, prints "Already set to start on login", and starts
            # nothing.  That is the same dead end as the enabled-but-not-running
            # hint below, one branch over, and it survived the fix to that one.
            "Fix that, then start it again: "
            + (RESTART_COMMAND if enabled else "kiln bridge start"),
        ]
    elif enabled:
        headline = "enabled, but not running"
        lines += [
            "Set to start on login, but not running right now.",
            # NOT `kiln bridge start`, which this state is precisely the one
            # that command refuses: it sees a login service and returns
            # "Already set to start on login" without starting anything, so
            # the advice read as help and did nothing.
            f"Start it now: {RESTART_COMMAND}   ·   Log: {_LOG_FILE}",
        ]
    else:
        return "off", [
            "Turn it on for good:  kiln bridge enable   (starts now + every login)",
            "Just this session:    kiln bridge start",
        ]

    if restarts and last_exit_at is not None:
        # A crash the user never saw, said out loud.  The printer keeps
        # printing through this (the machine owns the job) and the bridge is
        # back, but "it crashed and recovered" and "it never crashed" are not
        # the same fact and should not read the same.
        elapsed = _ago(now - last_exit_at)
        when = elapsed if elapsed == "just now" else f"{elapsed} ago"
        lines.append(
            f"Recovered from a crash {when} "
            f"({restarts} restart{'s' if restarts != 1 else ''} this run)."
        )

    if connected or running:
        # Only while something is actually up.  "enabled, but not running"
        # reaches this tail too, and there the note would be wrong twice over:
        # no process is holding old code, and the start it is about to get
        # picks up whatever is installed on its own.
        lines.extend(version_lines)

    if enabled:
        lines.append("Starts automatically on every login.")
    else:
        if supervised:
            lines.append("Restarts itself if it crashes — but not after a logout or reboot.")
        lines.append("Make it automatic: kiln bridge enable")
    return headline, lines


# ---------------------------------------------------------------------------
# Process supervision (manual `start` / `stop`)
# ---------------------------------------------------------------------------


def _pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        return _pid_alive_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else — still "alive"
    except OSError:
        return False
    return True


def _pid_alive_windows(pid: int) -> bool:
    """Probe a pid WITHOUT signalling it.

    ``os.kill(pid, 0)`` is NOT a probe on Windows — any signal other than the
    two console events unconditionally TerminateProcess()es the target, so the
    POSIX idiom would kill the bridge just by asking about it.  Use a
    query-only process handle instead.
    """
    import ctypes  # noqa: PLC0415 — Windows-only path

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _running_pid() -> int | None:
    """The live bridge pid from the state file, or ``None`` if not running."""
    pid = read_bridge_state().get("pid")
    if isinstance(pid, int) and _pid_alive(pid):
        return pid
    return None


def _installed_version() -> str:
    """What a bridge started right now would run — i.e. what is on disk.

    This process was launched seconds ago, so its import IS the current disk
    contents; the daemon's is whatever disk held when IT started.  Same
    question asked of two processes, which is exactly what makes the two
    answers comparable.
    """
    try:
        from kiln import __version__ as _v  # noqa: PLC0415

        return str(_v)
    except Exception:  # noqa: BLE001 -- a version we cannot read is simply no news
        return ""


def _latest_published_version() -> str | None:
    """The newest release on PyPI when we happen to know it, else ``None``.

    Cache-backed and non-blocking by construction — :func:`check_for_update`
    reads the shared 24h cache and warms it in a daemon thread — and honours
    the ``KILN_NO_UPDATE_CHECK`` / ``KILN_OFFLINE`` opt-out by returning
    nothing.  That opt-out costs the user only this line: the restart-pending
    half needs no network and is reported either way.
    """
    try:
        from kiln.version_check import check_for_update  # noqa: PLC0415

        info = check_for_update()
        latest = (info or {}).get("latest")
        return latest if isinstance(latest, str) else None
    except Exception:  # noqa: BLE001 -- status must never fail over a nudge
        return None


def _running_supervisor_pid() -> int | None:
    """The live supervisor pid, or ``None``.

    A recorded pid that is no longer alive means the supervisor was itself
    killed (or the machine went down) — the bridge is unsupervised from here
    on, and status says so rather than claiming a protection that is gone.
    """
    pid = supervisor_pid()
    if pid is not None and _pid_alive(pid):
        return pid
    return None


def _anything_running() -> bool:
    """Whether a bridge run is in flight — the bridge, its supervisor, or both.

    Both, because between a crash and its restart the bridge pid is briefly
    absent while the run is very much still going.  Asking only about that one
    would read a live run as a dead one, and `start` would put a second
    supervisor over the top of the first.
    """
    return _running_pid() is not None or _running_supervisor_pid() is not None


def _spawn_supervised_bridge() -> int:
    """Launch a supervised bridge detached from this terminal; return the
    supervisor's pid.

    The supervisor, not the bridge, because a bare background process is
    exactly what left the bridge dead after a ``kill -9`` mid-print: nothing
    restarted it and nothing said so.  Its pid is the one to stop — killing it
    stops the child too, whereas killing the child alone just gets it restarted.
    """
    os.makedirs(os.path.dirname(_LOG_FILE), exist_ok=True)
    log = open(_LOG_FILE, "a", encoding="utf-8")  # noqa: SIM115 — handed to the child
    kwargs: dict = {"stdout": log, "stderr": log, "stdin": subprocess.DEVNULL}
    if os.name == "posix":
        kwargs["start_new_session"] = True  # leave the controlling terminal
    else:  # Windows: DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        kwargs["creationflags"] = 0x00000008 | 0x00000200
    proc = subprocess.Popen(
        [sys.executable, "-m", "kiln.bridge_supervisor"], **kwargs
    )
    return proc.pid


def _terminate(pid: int) -> None:
    """SIGTERM *pid* and wait up to ~3s for it to go."""
    with contextlib.suppress(OSError):
        os.kill(pid, signal.SIGTERM)
    for _ in range(30):
        if not _pid_alive(pid):
            return
        time.sleep(0.1)


def _stop_process() -> bool:
    """Stop the supervisor first, then the bridge itself.

    Order is load-bearing: SIGTERM the bridge while its supervisor is still
    watching and the supervisor does its job and starts a new one, so ``kiln
    bridge stop`` would report success over a bridge that is still running.
    """
    supervisor = _running_supervisor_pid()
    if supervisor is not None:
        _terminate(supervisor)
    clear_supervisor_state()

    pid = _running_pid()
    if pid is None:
        clear_bridge_state()
        return supervisor is not None
    _terminate(pid)
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
# Login service (enable / disable) — launchd on macOS, systemd --user on
# Linux, HKCU Run key on Windows.  All three are per-user, no elevation.
# ---------------------------------------------------------------------------


def _plist_path() -> str:
    return os.path.expanduser(f"~/Library/LaunchAgents/{_LABEL}.plist")


def _systemd_path() -> str:
    return os.path.expanduser(f"~/.config/systemd/user/{_UNIT}")


def _run_key_installed() -> bool:
    import winreg  # noqa: PLC0415 — Windows-only path

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            winreg.QueryValueEx(key, _RUN_VALUE)
        return True
    except OSError:
        return False


def _install_run_key() -> tuple[bool, str]:
    import winreg  # noqa: PLC0415 — Windows-only path

    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            winreg.SetValueEx(
                key, _RUN_VALUE, 0, winreg.REG_SZ,
                _render_run_command(_windows_pythonw()),
            )
        return True, ""
    except OSError as exc:
        return False, str(exc)


def _remove_run_key() -> None:
    import winreg  # noqa: PLC0415 — Windows-only path

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.DeleteValue(key, _RUN_VALUE)
    except OSError:
        pass


def _install_protocol_handler() -> tuple[bool, str]:
    """Register ``kiln://`` under HKCU so Windows routes a clicked link to
    ``kiln bridge handle-uri``. Idempotent (safe to call on every ``enable``);
    never removed by ``disable`` — a stale-but-harmless registration is
    exactly what makes a FUTURE re-enable a real one click.
    """
    import winreg  # noqa: PLC0415 — Windows-only path

    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _PROTOCOL_CLASS_KEY) as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, "URL:Kiln Bridge Protocol")
            winreg.SetValueEx(key, "URL Protocol", 0, winreg.REG_SZ, "")
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _PROTOCOL_COMMAND_KEY) as key:
            winreg.SetValueEx(
                key, "", 0, winreg.REG_SZ,
                _render_protocol_command(_windows_pythonw()),
            )
        return True, ""
    except OSError as exc:
        return False, str(exc)


def _protocol_handler_installed() -> bool:
    import winreg  # noqa: PLC0415 — Windows-only path

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _PROTOCOL_COMMAND_KEY):
            pass
        return True
    except OSError:
        return False


def _service_installed() -> bool:
    if sys.platform == "darwin":
        return os.path.exists(_plist_path())
    if sys.platform.startswith("linux"):
        return os.path.exists(_systemd_path())
    if sys.platform == "win32":
        return _run_key_installed()
    return False


def _os_supervises_the_bridge() -> bool:
    """True where the login service itself brings a dead bridge back.

    The axis a restart turns on, and it is NOT "is a service installed".
    launchd (``KeepAlive``) and systemd (``Restart=always``) watch the process
    and respawn it, so restarting there means ending the process and letting
    the OS do what it already does.  The Windows Run key launches at login and
    never looks again — which is exactly why ``enable`` starts our own
    supervisor there — so a Windows bridge, installed or not, is cycled the
    same way a session one is.
    """
    return _service_installed() and sys.platform != "win32"


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
    if sys.platform == "win32":
        # Run key = start at login (per-user, no elevation, windowless via
        # pythonw).  It launches and forgets, so what it launches is our own
        # supervisor — the bridge's internal reconnect loop covers a dropped
        # socket but cannot outlive the process it runs in.
        ok, detail = _install_run_key()
        if not ok:
            return False, detail
        try:
            _spawn_supervised_bridge()  # parity: enable starts it NOW, not just at login
        except OSError as exc:
            return False, f"installed for login, but starting now failed: {exc}"
        return True, ""
    return False, "unsupported-platform"


def _remove_service() -> None:
    """Stop + remove the login service (idempotent, best-effort)."""
    if sys.platform == "darwin":
        path = _plist_path()
        if os.path.exists(path):
            subprocess.run(["launchctl", "unload", "-w", path], capture_output=True, text=True)
            with contextlib.suppress(OSError):
                os.unlink(path)
    elif sys.platform.startswith("linux") and os.path.exists(_systemd_path()):
        subprocess.run(
            ["systemctl", "--user", "disable", "--now", _UNIT],
            capture_output=True,
            text=True,
        )
        with contextlib.suppress(OSError):
            os.unlink(_systemd_path())
        subprocess.run(
            ["systemctl", "--user", "daemon-reload"], capture_output=True, text=True
        )
    elif sys.platform == "win32":
        # The running process is stopped by the caller (`disable` calls
        # ``_stop_process``); this only removes the login entry.
        _remove_run_key()


def _preflight() -> None:
    """Fail-fast checks before we start dialing out."""
    bearer = _resolve_bearer()
    if not bearer.token:
        raise click.ClickException(
            (f"{bearer.detail}\n" if bearer.detail else "")
            + "Sign in so the relay can route to you:\n"
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
# First-run onboarding — the paste from kiln3d.com ends in `kiln bridge
# enable`, so this command is where a brand-new machine becomes a working
# print path.  Everything here reuses the engines that already exist
# (discovery, config, the model prompt, slicer detection); this file only
# sequences them and keeps quiet when there is nothing to do.
# ---------------------------------------------------------------------------

_MANUAL_SETUP_HINT = (
    "  Connect it by hand any time:\n"
    "    kiln discover                              (find its address)\n"
    "    kiln auth -n myprinter -h <address> --type <octoprint|moonraker|creality|bambu|...>"
)


def _onboarding_interactive() -> bool:
    """True when a human is at both ends — the only time we may prompt.

    The Windows ``kiln://`` relaunch is windowless and scripted runs have
    nobody to answer, so everything in this section becomes a no-op there:
    the bridge still turns on, and the printer can be connected later.
    """
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except Exception:
        return False


def _suggest_printer_name(display_name: str, printer_type: str) -> str:
    """A config-friendly slug from whatever the network called the machine."""
    import re as _re

    base = _re.sub(r"[^a-z0-9]+", "-", (display_name or "").lower()).strip("-")
    return base or printer_type


def _discovered_line(found: object) -> str:
    """One picker row: the advertised name when there is one, address always."""
    label = (getattr(found, "name", "") or "").strip() or found.printer_type
    port = getattr(found, "port", 0) or 0
    address = found.host if port in (0, 80) else f"{found.host}:{port}"
    return f"{label} — {address} ({found.printer_type})"


def _discovered_host(found: object) -> str:
    """The host string ``save_printer`` wants for this discovery result.

    HTTP backends keep their discovered port (Moonraker answers on 7125,
    4408, or 80 depending on the machine); Bambu and Elegoo speak
    MQTT/SDCP on fixed ports their adapters own, so they get the bare
    address — ``_normalize_host`` strips any scheme for them anyway.
    """
    if found.printer_type in ("bambu", "elegoo"):
        return found.host
    port = getattr(found, "port", 0) or 0
    return found.host if port in (0, 80) else f"{found.host}:{port}"


def _credential_prompts(printer_type: str, discovered_serial: str) -> dict:
    """Ask only for what this backend actually needs, nothing else."""
    extras: dict = {}
    if printer_type == "octoprint":
        extras["api_key"] = click.prompt(
            "  OctoPrint API key (OctoPrint → Settings → API)", default="", show_default=False
        ).strip() or None
    elif printer_type == "prusalink":
        extras["api_key"] = click.prompt(
            "  PrusaLink password (printer screen → Settings → Network)",
            default="", show_default=False,
        ).strip() or None
    elif printer_type == "duet":
        extras["api_key"] = click.prompt(
            "  Machine password (Enter if you never set one)", default="", show_default=False
        ).strip() or None
    elif printer_type == "bambu":
        extras["access_code"] = click.prompt(
            "  LAN access code (printer screen → Settings → WLAN)", default="", show_default=False
        ).strip() or None
        serial = click.prompt(
            "  Serial number", default=discovered_serial or "", show_default=bool(discovered_serial)
        ).strip()
        extras["serial"] = serial or None
    elif printer_type == "elegoo" and discovered_serial:
        extras["serial"] = discovered_serial
    return extras


def _offer_first_printer() -> None:
    """When no printer is saved yet, find one on the network and save it.

    This is the difference between "the bridge is on" and "the Print button
    works": the web's ``slice_and_print`` targets the active printer, so a
    bridge with an empty config connects fine and then has nothing to drive.
    Interactive terminals only; every exit path leaves enable free to finish.
    """
    try:
        from kiln.cli.config import list_printers

        if list_printers():
            return
    except Exception:
        return  # unreadable config is not this step's fight
    if not _onboarding_interactive():
        return

    from kiln.cli.discovery import discover_printers

    click.echo()
    click.echo(click.style("  No printer connected yet — let's find yours.", bold=True))
    click.echo("  Scanning your network (up to ~12 seconds)…")
    try:
        found = [p for p in discover_printers() if p.printer_type != "unknown"]
    except Exception:
        found = []

    if not found:
        click.echo("  Nothing answered. The printer may be off, asleep, or on a different Wi-Fi.")
        click.echo(_MANUAL_SETUP_HINT)
        click.echo("  Turning the bridge on anyway — connect the printer whenever you like.")
        return

    click.echo()
    if len(found) == 1:
        click.echo(f"  Found: {_discovered_line(found[0])}")
        if not click.confirm("  Use this printer?", default=True):
            click.echo(_MANUAL_SETUP_HINT)
            return
        chosen = found[0]
    else:
        click.echo("  Found on your network:")
        for i, p in enumerate(found, 1):
            click.echo(f"    {i}. {_discovered_line(p)}")
        raw = click.prompt(
            "  Which one is yours? (number, or Enter to skip)", default="", show_default=False
        ).strip()
        if not raw.isdigit() or not (1 <= int(raw) <= len(found)):
            click.echo(_MANUAL_SETUP_HINT)
            return
        chosen = found[int(raw) - 1]

    extras = _credential_prompts(chosen.printer_type, getattr(chosen, "serial", "") or "")

    # The model key turns on the safety stack (bed-fit, temperature limits);
    # skippable so a shy answer never strands the setup, and the prompt itself
    # says what skipping costs.
    from kiln.cli.printer_model_prompt import prompt_for_printer_model

    serial_hint = extras.get("serial") or getattr(chosen, "serial", "") or None
    model = prompt_for_printer_model(chosen.printer_type, serial_hint, allow_skip=True)

    from kiln.cli.config import save_printer

    name = _suggest_printer_name(getattr(chosen, "name", ""), chosen.printer_type)
    try:
        save_printer(
            name,
            chosen.printer_type,
            _discovered_host(chosen),
            printer_model=model,
            **extras,
        )
    except Exception as exc:
        click.echo(click.style(f"  Couldn't save that printer: {exc}", fg="yellow"))
        click.echo(_MANUAL_SETUP_HINT)
        return
    click.echo(
        click.style("  Saved ✓", fg="green")
        + f" — '{name}' is your active printer."
    )


def _slicer_note() -> None:
    """One honest line when printing would fail for want of a slicer.

    The bridge slices on this machine before it prints, so a missing slicer
    is the next wall the user would hit — after walking away believing setup
    was done.  Saying it now, with the fix, is the whole feature.  Never
    fatal: the bridge does more than print, and the note must not be able to
    break enable.
    """
    try:
        from kiln.slicer import SlicerNotFoundError, find_slicer

        find_slicer()
    except SlicerNotFoundError:
        from kiln.slicer import _INSTALL_SLICER

        click.echo(click.style("  One more thing — printing needs a slicer.", fg="yellow"))
        for line in _INSTALL_SLICER.splitlines():
            click.echo(f"  {line}")
        click.echo("  Install one and you're done — nothing to re-run.")
    except Exception:  # noqa: BLE001 — a broken probe must not break enable
        return


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@click.group()
def bridge() -> None:
    """Let kiln3d.com print to this machine's printers (opt-in, outbound-only)."""


@bridge.command()
def status() -> None:
    """Show whether the bridge is on, connected, and running current code."""
    st = read_bridge_state()
    sup = read_supervisor_state()
    running = _running_pid() is not None
    connected = bool(st.get("connected")) and running
    bearer = _resolve_bearer()
    last_exit = sup.get("last_exit") if isinstance(sup.get("last_exit"), dict) else {}
    enabled = _service_installed()
    version = describe_bridge_version(
        running=st.get("version"),
        installed=_installed_version(),
        latest=_latest_published_version(),
    )
    headline, lines = _describe_status(
        signed_in=bool(bearer.token),
        enabled=enabled,
        running=running,
        connected=connected,
        since=st.get("since"),
        now=time.time(),
        signin_detail=bearer.detail,
        supervised=_running_supervisor_pid() is not None,
        restarts=int(sup.get("restarts") or 0),
        last_exit_at=last_exit.get("at"),
        gave_up=bool(sup.get("gave_up")),
        version_lines=version.lines,
    )
    # Default terminal colour for the off/idle states — a fixed "white" is
    # invisible on a light-background terminal.
    dot = "green" if connected else ("yellow" if running else None)
    styled = click.style(headline, fg=dot) if dot else headline
    click.echo(click.style("Bridge: ", bold=True) + styled)
    for line in lines:
        click.echo(f"  {line}")


def _enable_bridge() -> None:
    """The core of ``enable`` — shared with the Windows ``kiln://`` handler
    (:func:`handle_uri`) so a deep-link click does exactly what typing the
    command would, not a second, drifting copy of the logic.
    """
    _preflight()
    _offer_first_printer()
    _stop_process()  # fold any manual run into the managed one — never two bridges
    ok, detail = _install_service()
    if not ok:
        if detail == "unsupported-platform":
            raise click.ClickException(
                "Starting on login isn't supported on this platform yet.\n"
                "Run it for this session instead: kiln bridge start"
            )
        raise click.ClickException(f"Couldn't enable the login service: {detail}")
    if sys.platform == "win32":
        # Best-effort: a failed registration must never fail `enable` itself
        # — the bridge is already running at this point regardless.
        _install_protocol_handler()
    click.echo(
        click.style("Bridge on.", fg="green")
        + " It starts automatically every time you log in."
    )
    if _await_connected():
        click.echo("  Connected ✓ — prints from kiln3d.com now reach this machine's printers.")
    else:
        click.echo("  Connecting… confirm with: kiln bridge status")
    _slicer_note()
    click.echo("  Turn it off any time: kiln bridge disable")


@bridge.command()
def enable() -> None:
    """Turn the bridge on for good — start now and on every login.

    On a machine with no printer saved yet, this is also the first-run walk:
    it scans the network for your printer, saves the one you pick, and points
    out a missing slicer — so the paste from kiln3d.com ends in one command
    and a working print path, not a bridge with nothing to drive.
    """
    _enable_bridge()


def parse_bridge_uri(uri: str) -> str | None:
    """Parse ``kiln://bridge/<action>`` into ``<action>``, or ``None`` if the
    URI isn't a recognised bridge action. Pure — no OS calls — so the exact
    strings a browser could hand a running app are fully unit-tested without
    ever registering a real handler.
    """
    parsed = urllib.parse.urlparse(uri)
    if parsed.scheme != "kiln" or parsed.netloc != "bridge":
        return None
    return parsed.path.lstrip("/") or None


@bridge.command(name="handle-uri", hidden=True)
@click.argument("uri")
def handle_uri(uri: str) -> None:
    """Internal: run the action a ``kiln://`` link was clicked for.

    Invoked by Windows (never typed by a person) when the user clicks a
    "Connect my printer" link on kiln3d.com — the one-click reconnect that
    only exists once ``enable`` has registered the handler at least once.
    An unrecognised action is ignored rather than erroring loudly: nobody is
    watching a console for this windowless launch.
    """
    if parse_bridge_uri(uri) == "enable":
        _enable_bridge()


@bridge.command()
def disable() -> None:
    """Turn the bridge off — stop now and stop starting on login."""
    was = (
        _service_installed()
        or _running_pid() is not None
        or _running_supervisor_pid() is not None
    )
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
        # Still a refusal — starting our own supervisor under launchd/systemd
        # would be two parents over one child, which is the whole reason this
        # branch exists.  But "it's managed for you" is only true while it is
        # actually up: said over a bridge that is DOWN it reads as an all-clear,
        # and the person typing `start` is typing it precisely because nothing
        # is running.  Point them at the verb that does start a managed bridge
        # rather than sending them away reassured.
        if not _anything_running():
            click.echo("Set to start on login, but it isn't running right now.")
            click.echo(f"  Start it: {RESTART_COMMAND}   ·   More: kiln bridge status")
            return
        click.echo("Already set to start on login — it's managed for you.")
        click.echo("  See it: kiln bridge status   ·   Turn off: kiln bridge disable")
        return
    if _anything_running():
        click.echo("Bridge is already running.  See: kiln bridge status")
        return
    _preflight()
    _offer_first_printer()
    pid = _spawn_supervised_bridge()
    if _await_connected():
        click.echo(click.style("Bridge on ✓", fg="green") + f" (pid {pid}) — connected.")
        click.echo("  Prints from kiln3d.com now reach this machine's printers.")
    else:
        click.echo(click.style("Bridge started", fg="green") + f" (pid {pid}) — connecting…")
        click.echo("  Confirm with: kiln bridge status")
    # Say what this does and does not survive, then offer the thing that
    # covers the rest.  Installing a login item because someone typed `start`
    # would be a background process the user never agreed to and would find
    # later by accident; an offer costs one line and leaves the choice theirs.
    click.echo("  It restarts itself if it crashes, but not after you log out or reboot.")
    click.echo("  Survive a reboot too: kiln bridge enable   ·   Stop it: kiln bridge stop")


#: How long ``restart`` waits for the bridge to come back.  launchd throttles
#: respawns to one per ``ThrottleInterval`` (10s by default) and systemd waits
#: ``RestartSec=3``, so a bridge cycled twice in quick succession can take
#: about ten seconds.  Measured on macOS: one that had been up longer than the
#: throttle came back in about a second.
_RESTART_WAIT_S = 12.0


@bridge.command()
def restart() -> None:
    """Restart the bridge — the way to pick up a newer Kiln.

    One verb over the two-command pairs this used to take, because which pair
    worked depended on how the bridge was supervised and the wrong one does
    nothing at all.  That is a fact about our own plumbing and there is no
    reason a user should have to hold it.

    A bridge keeps the code it imported at boot, and neither launchd nor
    systemd restarts a process merely for being old, so this is what a
    ``pip install --upgrade kiln3d`` needs before the daemon is actually
    running the version you installed.  It does NOT install anything — see
    :mod:`kiln.bridge_version` for why that stays a separate, asked-for act.
    """
    running = _running_pid()
    supervised = _running_supervisor_pid()

    if running is None and supervised is None:
        if not _service_installed():
            click.echo("Bridge isn't running, so there's nothing to restart.")
            click.echo("  Start it: kiln bridge start   ·   Or for good: kiln bridge enable")
            return
        # Set to start on login but nothing is up — the honest action is to
        # start it through the login service, which is also the one thing
        # `kiln bridge start` refuses to do for a managed bridge.
        _preflight()
        ok, detail = _install_service()
        if not ok:
            raise click.ClickException(f"Couldn't start the login service: {detail}")
        _report_restart("Bridge started", interrupted=False)
        return

    _preflight()
    if _os_supervises_the_bridge():
        # End the process and let launchd/systemd do what they already do for
        # a bridge that dies.  Deliberately NOT a reinstall of the service:
        # on Linux `systemctl enable --now` only STARTS an inactive unit and
        # would leave a running one on its old code — the exact failure this
        # whole command exists to prevent.  The state file is left alone; the
        # replacement process rewrites it, and clearing it here would race.
        if running is not None:
            _terminate(running)
    else:
        # Our supervisor (a session `start`, or Windows `enable`).  A full
        # stop-and-respawn rather than killing the child and letting the
        # supervisor catch it: a deliberate restart is not a crash, and it
        # should not spend the crash-loop budget that exists to notice a
        # genuinely broken install.
        _stop_process()
        _spawn_supervised_bridge()

    _report_restart("Bridge restarted", interrupted=True)


def _report_restart(headline: str, *, interrupted: bool) -> None:
    """Say whether it came back, and what it came back as."""
    if _await_connected(timeout=_RESTART_WAIT_S):
        version = read_bridge_state().get("version")
        running_now = f" — now running Kiln {version}" if version else ""
        click.echo(click.style(f"{headline} ✓", fg="green") + f"{running_now}, connected.")
    else:
        click.echo(click.style(f"{headline}.", fg="green") + " Connecting…")
        click.echo("  Confirm with: kiln bridge status")
    if interrupted:
        # A print in progress is owned by the machine, not by us: it keeps
        # going through this.  What drops is the web's route to it, briefly.
        # Only worth saying when there was something to interrupt.
        click.echo("  Your printer keeps printing; the link from kiln3d.com blinks.")


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
    """Attach ``kiln bridge {status,start,stop,restart,enable,disable}``.

    Called unconditionally from ``kiln.cli.main`` — the bridge ships in the
    public package and depends on nothing proprietary.
    """
    cli_group.add_command(bridge)
