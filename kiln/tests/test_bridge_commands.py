"""``kiln bridge`` CLI — the opt-in service surface, tested without hardware.

Covers the honest-status state machine, the login-service file renders, the
liveness state round-trip, and the opt-in gate (nothing dials out without a
signed-in account).  The launchctl/systemctl side effects are not invoked here.
"""
from __future__ import annotations

import os

from click.testing import CliRunner

import kiln.bridge_client as bc
import kiln.cli.bridge_commands as bcmd
from kiln.cli.bridge_commands import (
    _ago,
    _describe_status,
    _render_plist,
    _render_protocol_command,
    _render_systemd_unit,
    bridge,
    parse_bridge_uri,
)

# --- liveness state round-trip --------------------------------------------


def test_state_roundtrip_and_streak(tmp_path, monkeypatch):
    monkeypatch.setattr(bc, "_STATE_FILE", str(tmp_path / "bridge.state"))
    assert bc.read_bridge_state() == {}

    bc.write_bridge_state(connected=False)
    st = bc.read_bridge_state()
    assert st["pid"] == os.getpid()
    assert st["connected"] is False
    assert st["since"] is None

    bc.write_bridge_state(connected=True)
    since = bc.read_bridge_state()["since"]
    assert since is not None

    # A reconnect while already connected preserves the original streak start.
    bc.write_bridge_state(connected=True)
    assert bc.read_bridge_state()["since"] == since

    # Dropping the link clears the streak.
    bc.write_bridge_state(connected=False)
    assert bc.read_bridge_state()["since"] is None

    bc.clear_bridge_state()
    assert bc.read_bridge_state() == {}


def test_read_state_survives_corrupt_file(tmp_path, monkeypatch):
    path = tmp_path / "bridge.state"
    path.write_text("not json{")
    monkeypatch.setattr(bc, "_STATE_FILE", str(path))
    assert bc.read_bridge_state() == {}  # never raises


# --- honest status state machine ------------------------------------------


def test_describe_status_matrix():
    now = 1000.0

    head, lines = _describe_status(
        signed_in=False, enabled=False, running=False,
        connected=False, since=None, now=now,
    )
    assert head == "signed out"
    assert any("signin" in ln for ln in lines)

    head, lines = _describe_status(
        signed_in=True, enabled=False, running=False,
        connected=False, since=None, now=now,
    )
    assert head == "off"
    assert any("enable" in ln for ln in lines)

    head, lines = _describe_status(
        signed_in=True, enabled=True, running=True,
        connected=True, since=now - 120, now=now,
    )
    assert head == "on, connected"
    assert any("2m" in ln for ln in lines)
    assert any("login" in ln for ln in lines)

    head, _ = _describe_status(
        signed_in=True, enabled=True, running=True,
        connected=False, since=None, now=now,
    )
    assert head == "on, connecting…"

    head, _ = _describe_status(
        signed_in=True, enabled=True, running=False,
        connected=False, since=None, now=now,
    )
    assert head == "enabled, but not running"


def test_ago():
    assert _ago(5) == "just now"
    assert _ago(300) == "5m"
    assert _ago(8040) == "2h 14m"


# --- login-service file renders -------------------------------------------


def test_plist_render():
    plist = _render_plist("/opt/py/bin/python3", "/home/u/.kiln/bridge.log")
    assert "com.kiln3d.bridge" in plist
    assert "<string>/opt/py/bin/python3</string>" in plist
    assert "kiln.bridge_client" in plist
    assert "<key>RunAtLoad</key><true/>" in plist
    assert "<key>KeepAlive</key><true/>" in plist
    assert "/home/u/.kiln/bridge.log" in plist


def test_systemd_unit_render():
    unit = _render_systemd_unit("/opt/py/bin/python3")
    assert "ExecStart=/opt/py/bin/python3 -m kiln.bridge_client" in unit
    assert "Restart=always" in unit
    assert "WantedBy=default.target" in unit


def test_run_command_render_quotes_the_interpreter():
    cmd = bcmd._render_run_command(r"C:\Py 3.12\pythonw.exe")
    assert cmd == '"C:\\Py 3.12\\pythonw.exe" -m kiln.bridge_supervisor'


def test_only_the_unsupervising_login_mechanism_gets_our_supervisor():
    """launchd and systemd supervise; the Windows Run key does not.

    So the plist and the unit launch the bridge directly and the Run key
    launches the supervisor.  Stacking ours under launchd or systemd would put
    two parents on one child, each entitled to restart it.
    """
    assert "kiln.bridge_client" in _render_plist("/py", "/log")
    assert "kiln.bridge_supervisor" not in _render_plist("/py", "/log")

    assert "kiln.bridge_client" in _render_systemd_unit("/py")
    assert "kiln.bridge_supervisor" not in _render_systemd_unit("/py")

    assert "kiln.bridge_supervisor" in bcmd._render_run_command("py.exe")


def test_windows_pythonw_prefers_the_windowless_interpreter(monkeypatch):
    import sys as _sys

    monkeypatch.setattr(_sys, "executable", "/py/python.exe")
    monkeypatch.setattr(bcmd.os.path, "exists", lambda p: p.endswith("pythonw.exe"))
    assert bcmd._windows_pythonw() == "/py/pythonw.exe"

    monkeypatch.setattr(bcmd.os.path, "exists", lambda p: False)
    assert bcmd._windows_pythonw() == "/py/python.exe"


# --- Windows dispatch (the win32 branches, without a registry) -------------


def test_win32_dispatch(monkeypatch):
    import sys as _sys

    monkeypatch.setattr(_sys, "platform", "win32")

    # _service_installed → Run-key probe
    monkeypatch.setattr(bcmd, "_run_key_installed", lambda: True)
    assert bcmd._service_installed() is True

    # _pid_alive → query-only probe (never os.kill on Windows)
    probed = []
    monkeypatch.setattr(bcmd, "_pid_alive_windows", lambda pid: probed.append(pid) or True)
    assert bcmd._pid_alive(4242) is True
    assert probed == [4242]

    # _install_service → Run key + immediate spawn
    spawned = []
    monkeypatch.setattr(bcmd, "_install_run_key", lambda: (True, ""))
    monkeypatch.setattr(bcmd, "_spawn_supervised_bridge", lambda: spawned.append(1) or 999)
    ok, detail = bcmd._install_service()
    assert ok and detail == "" and spawned == [1]

    # _remove_service → Run-key removal
    removed = []
    monkeypatch.setattr(bcmd, "_remove_run_key", lambda: removed.append(1))
    bcmd._remove_service()
    assert removed == [1]


# --- CLI wiring + the opt-in gate -----------------------------------------


def test_group_exposes_all_verbs():
    # handle-uri is hidden from --help (it's OS-invoked, never typed) but
    # still a real registered command — `hidden=True` only affects listing.
    assert set(bridge.commands) == {
        "status", "start", "stop", "restart", "enable", "disable", "handle-uri",
    }
    assert bridge.commands["handle-uri"].hidden is True


def test_start_and_enable_refuse_without_a_signed_in_account(monkeypatch):
    monkeypatch.setattr(bcmd, "_read_license", lambda: "")
    monkeypatch.setattr(bcmd, "_service_installed", lambda: False)
    monkeypatch.setattr(bcmd, "_running_pid", lambda: None)
    runner = CliRunner()

    out = runner.invoke(bridge, ["start"])
    assert out.exit_code != 0
    assert "signin" in out.output.lower()

    out = runner.invoke(bridge, ["enable"])
    assert out.exit_code != 0
    assert "signin" in out.output.lower()


def test_status_reads_off_when_signed_in_and_idle(monkeypatch):
    monkeypatch.setattr(bcmd, "_read_license", lambda: "lic")
    monkeypatch.setattr(bcmd, "_service_installed", lambda: False)
    monkeypatch.setattr(bcmd, "read_bridge_state", lambda: {})
    monkeypatch.setattr(bcmd, "_running_pid", lambda: None)
    out = CliRunner().invoke(bridge, ["status"])
    assert out.exit_code == 0
    assert "off" in out.output


def test_stop_defers_to_disable_when_service_is_enabled(monkeypatch):
    monkeypatch.setattr(bcmd, "_service_installed", lambda: True)
    out = CliRunner().invoke(bridge, ["stop"])
    assert out.exit_code == 0
    assert "disable" in out.output


def test_await_connected(monkeypatch):
    monkeypatch.setattr(bcmd, "read_bridge_state", lambda: {"connected": True})
    monkeypatch.setattr(bcmd, "_running_pid", lambda: 123)
    assert bcmd._await_connected(timeout=1.0) is True

    monkeypatch.setattr(bcmd, "read_bridge_state", lambda: {"connected": False})
    assert bcmd._await_connected(timeout=0.0) is False


# --- Windows `kiln://` protocol handler -------------------------------------
#
# The one-click "Connect my printer" path: a browser click hands the OS
# `kiln://bridge/enable`, Windows launches the registered command, which
# dispatches back into the SAME `_enable_bridge()` the CLI verb runs. Only
# ever registered as a side effect of a successful `enable` — never the
# very-first connection, which still needs one typed/pasted command, same as
# macOS and Linux.


def test_render_protocol_command_quotes_the_interpreter_and_passes_percent1():
    cmd = _render_protocol_command(r"C:\Py 3.12\pythonw.exe")
    assert cmd == '"C:\\Py 3.12\\pythonw.exe" -m kiln.cli.main bridge handle-uri "%1"'


class TestParseBridgeUri:
    def test_the_one_action_that_exists(self):
        assert parse_bridge_uri("kiln://bridge/enable") == "enable"

    def test_wrong_scheme_is_rejected(self):
        assert parse_bridge_uri("http://bridge/enable") is None

    def test_wrong_host_is_rejected(self):
        assert parse_bridge_uri("kiln://printer/enable") is None

    def test_missing_action_is_rejected(self):
        assert parse_bridge_uri("kiln://bridge/") is None
        assert parse_bridge_uri("kiln://bridge") is None

    def test_an_unrecognised_action_still_parses_the_string(self):
        # parse_bridge_uri only extracts the action; deciding what to DO with
        # an unknown one is the caller's job (handle_uri ignores it).
        assert parse_bridge_uri("kiln://bridge/self-destruct") == "self-destruct"

    def test_garbage_input_never_raises(self):
        assert parse_bridge_uri("") is None
        assert parse_bridge_uri("not a uri at all") is None


def test_win32_protocol_handler_dispatch(monkeypatch):
    import sys as _sys

    monkeypatch.setattr(_sys, "platform", "win32")

    written = {}

    class _FakeKey:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_create_key(hive, path):
        written.setdefault(path, {})
        return _FakeKey()

    def fake_set_value(key, name, _res, _type, value):
        # The two CreateKey calls happen in sequence; attribute the write to
        # whichever path was created last (good enough for this fake).
        written[list(written)[-1]][name] = value

    import types
    fake_winreg = types.SimpleNamespace(
        HKEY_CURRENT_USER="HKCU",
        REG_SZ="REG_SZ",
        CreateKey=fake_create_key,
        SetValueEx=fake_set_value,
    )
    monkeypatch.setitem(_sys.modules, "winreg", fake_winreg)
    monkeypatch.setattr(bcmd, "_windows_pythonw", lambda: "C:\\py\\pythonw.exe")

    ok, detail = bcmd._install_protocol_handler()
    assert ok and detail == ""
    assert written[bcmd._PROTOCOL_CLASS_KEY]["URL Protocol"] == ""
    assert "handle-uri" in written[bcmd._PROTOCOL_COMMAND_KEY][""]


def test_enable_registers_the_protocol_handler_on_windows_only(monkeypatch):
    import sys as _sys

    monkeypatch.setattr(bcmd, "_preflight", lambda: None)
    monkeypatch.setattr(bcmd, "_stop_process", lambda: False)
    monkeypatch.setattr(bcmd, "_install_service", lambda: (True, ""))
    monkeypatch.setattr(bcmd, "_await_connected", lambda: True)

    registered = []
    monkeypatch.setattr(bcmd, "_install_protocol_handler", lambda: registered.append(1))

    monkeypatch.setattr(_sys, "platform", "darwin")
    bcmd._enable_bridge()
    assert registered == []  # macOS never attempts registry work

    monkeypatch.setattr(_sys, "platform", "win32")
    bcmd._enable_bridge()
    assert registered == [1]


def test_handle_uri_command_dispatches_enable(monkeypatch):
    calls = []
    monkeypatch.setattr(bcmd, "_enable_bridge", lambda: calls.append("enabled"))

    out = CliRunner().invoke(bridge, ["handle-uri", "kiln://bridge/enable"])
    assert out.exit_code == 0
    assert calls == ["enabled"]


def test_handle_uri_command_ignores_unrecognised_uris(monkeypatch):
    calls = []
    monkeypatch.setattr(bcmd, "_enable_bridge", lambda: calls.append("enabled"))

    out = CliRunner().invoke(bridge, ["handle-uri", "kiln://bridge/wipe-everything"])
    assert out.exit_code == 0  # never errors loudly — nobody's watching a console
    assert calls == []


# --- crash supervision: `start` survives a kill, and status admits it -------
#
# Measured 2026-08-11: `kill -9` on the bridge mid-print left the printer
# printing and the bridge dead, and nothing anywhere said so.  `start` now runs
# the bridge under `kiln.bridge_supervisor`; these cover the CLI half of that —
# the stop ordering, the double-start guard, and the two states status could
# not previously describe.


def test_status_admits_a_crash_it_recovered_from():
    """"crashed and came back" and "never crashed" are not the same fact."""
    now = 1000.0
    _, lines = _describe_status(
        signed_in=True, enabled=False, running=True, connected=True,
        since=now - 60, now=now,
        supervised=True, restarts=1, last_exit_at=now - 300,
    )
    assert any("Recovered from a crash 5m ago" in ln for ln in lines)
    assert any("1 restart " in ln for ln in lines)
    # And it is honest about the limit of what `start` bought you.
    assert any("not after a logout or reboot" in ln for ln in lines)


def test_status_does_not_invent_a_crash_that_never_happened():
    now = 1000.0
    _, lines = _describe_status(
        signed_in=True, enabled=False, running=True, connected=True,
        since=now - 60, now=now, supervised=True,
    )
    assert not any("Recovered" in ln for ln in lines)


def test_status_explains_a_bridge_that_gave_up():
    """Before this, a bridge that had tried and failed read exactly like one
    that had simply never been switched on."""
    head, lines = _describe_status(
        signed_in=True, enabled=False, running=False, connected=False,
        since=None, now=1000.0, gave_up=True,
    )
    assert head == "off after repeated crashes"
    assert any("kept stopping" in ln for ln in lines)
    assert any(bcmd._LOG_FILE in ln for ln in lines)


def test_stop_kills_the_supervisor_before_the_bridge(monkeypatch):
    """Order is load-bearing.  SIGTERM the bridge while its supervisor is still
    watching and the supervisor does its job — a new bridge — so `stop` would
    report success over a bridge that is still running.
    """
    killed: list[int] = []
    monkeypatch.setattr(bcmd, "_running_supervisor_pid", lambda: 111)
    monkeypatch.setattr(bcmd, "_running_pid", lambda: 222)
    monkeypatch.setattr(bcmd, "_terminate", killed.append)
    monkeypatch.setattr(bcmd, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(bcmd, "clear_supervisor_state", lambda: None)
    monkeypatch.setattr(bcmd, "clear_bridge_state", lambda: None)

    assert bcmd._stop_process() is True
    assert killed == [111, 222], "the supervisor must be stopped first"


def test_stop_reports_success_when_only_the_supervisor_was_up(monkeypatch):
    """Between a crash and its restart there is a supervisor and no bridge."""
    monkeypatch.setattr(bcmd, "_running_supervisor_pid", lambda: 111)
    monkeypatch.setattr(bcmd, "_running_pid", lambda: None)
    monkeypatch.setattr(bcmd, "_terminate", lambda _pid: None)
    monkeypatch.setattr(bcmd, "clear_supervisor_state", lambda: None)
    monkeypatch.setattr(bcmd, "clear_bridge_state", lambda: None)

    assert bcmd._stop_process() is True


def test_start_will_not_stack_a_second_supervisor(monkeypatch):
    """Same window: the bridge pid is briefly absent while the run continues.
    Checking only that one would start a second supervisor over the first.
    """
    monkeypatch.setattr(bcmd, "_read_license", lambda: "lic")
    monkeypatch.setattr(bcmd, "_service_installed", lambda: False)
    monkeypatch.setattr(bcmd, "_running_pid", lambda: None)
    monkeypatch.setattr(bcmd, "_running_supervisor_pid", lambda: 111)
    spawned: list[int] = []
    monkeypatch.setattr(bcmd, "_spawn_supervised_bridge", lambda: spawned.append(1) or 9)

    out = CliRunner().invoke(bridge, ["start"])
    assert out.exit_code == 0
    assert "already running" in out.output
    assert spawned == []


def test_start_offers_enable_rather_than_installing_it(monkeypatch):
    """A login item the user never agreed to is one they find by accident."""
    monkeypatch.setattr(bcmd, "_read_license", lambda: "lic")
    monkeypatch.setattr(bcmd, "_service_installed", lambda: False)
    monkeypatch.setattr(bcmd, "_running_pid", lambda: None)
    monkeypatch.setattr(bcmd, "_running_supervisor_pid", lambda: None)
    monkeypatch.setattr(bcmd, "_await_connected", lambda: True)
    monkeypatch.setattr(bcmd, "_spawn_supervised_bridge", lambda: 777)
    installed: list[int] = []
    monkeypatch.setattr(bcmd, "_install_service", lambda: installed.append(1) or (True, ""))

    out = CliRunner().invoke(bridge, ["start"])
    assert out.exit_code == 0
    assert installed == [], "`start` silently installed a login item"
    assert "kiln bridge enable" in out.output
    assert "restarts itself if it crashes" in out.output


def test_status_reads_the_supervisor_state_it_finds(tmp_path, monkeypatch):
    """End to end through the real files: a give-up on disk reaches the user."""
    import kiln.bridge_supervisor as bsup

    monkeypatch.setattr(bsup, "_SUPERVISOR_STATE", str(tmp_path / "bridge.supervisor"))
    bsup.update_supervisor_state(gave_up=True, last_exit={"code": 1, "at": 5.0})
    monkeypatch.setattr(bcmd, "_read_license", lambda: "lic")
    monkeypatch.setattr(bcmd, "_service_installed", lambda: False)
    monkeypatch.setattr(bcmd, "read_bridge_state", lambda: {})
    monkeypatch.setattr(bcmd, "_running_pid", lambda: None)

    out = CliRunner().invoke(bridge, ["status"])
    assert out.exit_code == 0
    assert "off after repeated crashes" in out.output


def test_a_give_up_outranks_enabled_but_not_running():
    """On Windows `enable` is supervised by us, so it can reach the give-up
    state — and "enabled, but not running" withholds the half we know."""
    head, lines = _describe_status(
        signed_in=True, enabled=True, running=False, connected=False,
        since=None, now=1000.0, gave_up=True,
    )
    assert head == "off after repeated crashes"
    assert any("kept stopping" in ln for ln in lines)


def test_a_very_recent_crash_reads_like_english():
    now = 1000.0
    _, lines = _describe_status(
        signed_in=True, enabled=False, running=True, connected=True,
        since=now, now=now, supervised=True, restarts=1, last_exit_at=now - 5,
    )
    line = next(ln for ln in lines if "Recovered" in ln)
    assert "just now ago" not in line
    assert line == "Recovered from a crash just now (1 restart this run)."


# --- version currency: the daemon can be older than the machine it runs on --
#
# A bridge under launchd holds the code it imported at boot.  `pip install
# --upgrade kiln3d` rewrites the files and launchd, which restarts a bridge
# that DIES and not one that is merely old, leaves it serving the old modules
# indefinitely.  The relay is shown the running version and every local command
# reports the installed one; both are right and nothing compared them.


def test_status_says_when_the_daemon_is_older_than_the_install():
    now = 1000.0
    _, lines = _describe_status(
        signed_in=True, enabled=True, running=True, connected=True,
        since=now - 60, now=now,
        version_lines=["Running Kiln 1.2.0, but 1.3.2 is installed here.",
                       "Pick up the newer one: kiln bridge restart"],
    )
    assert any("1.2.0" in ln and "1.3.2" in ln for ln in lines)
    assert any("kiln bridge restart" in ln for ln in lines)


def test_version_news_is_withheld_from_a_bridge_that_is_not_running():
    """Both things it can say are about a live process: an off bridge has
    nothing to restart, and its next start picks up the new code by itself.
    Under an off bridge the note would be a second errand competing with the
    only one that matters.
    """
    now = 1000.0
    noise = ["Running Kiln 1.2.0, but 1.3.2 is installed here."]

    head, lines = _describe_status(
        signed_in=True, enabled=False, running=False, connected=False,
        since=None, now=now, version_lines=noise,
    )
    assert head == "off"
    assert not any("1.2.0" in ln for ln in lines)

    head, lines = _describe_status(
        signed_in=True, enabled=True, running=False, connected=False,
        since=None, now=now, version_lines=noise,
    )
    assert head == "enabled, but not running"
    assert not any("1.2.0" in ln for ln in lines)

    head, lines = _describe_status(
        signed_in=False, enabled=False, running=False, connected=False,
        since=None, now=now, version_lines=noise,
    )
    assert head == "signed out"
    assert not any("1.2.0" in ln for ln in lines)

    head, lines = _describe_status(
        signed_in=True, enabled=False, running=False, connected=False,
        since=None, now=now, gave_up=True, version_lines=noise,
    )
    assert head == "off after repeated crashes"
    assert not any("1.2.0" in ln for ln in lines)


def test_a_current_bridge_says_nothing_about_versions():
    now = 1000.0
    _, lines = _describe_status(
        signed_in=True, enabled=True, running=True, connected=True,
        since=now - 60, now=now, version_lines=(),
    )
    assert not any("Kiln 1." in ln for ln in lines)


def test_the_running_bridge_records_the_version_it_loaded(tmp_path, monkeypatch):
    """The only place that fact exists on the machine."""
    monkeypatch.setattr(bc, "_STATE_FILE", str(tmp_path / "bridge.state"))
    monkeypatch.setattr(bc, "_running_version", lambda: "1.2.0")

    bc.write_bridge_state(connected=True)
    assert bc.read_bridge_state()["version"] == "1.2.0"


def test_the_relay_and_the_state_file_cannot_disagree(monkeypatch):
    """One helper feeds the handshake header and the state file, so the
    version the server sees and the version status reports are one fact."""
    monkeypatch.setattr(bc, "_running_version", lambda: "9.9.9")
    client = bc.BridgeClient(license_key="lic", call_tool=lambda *_a: None,
                             fetch_artifact=lambda _t: "")
    assert client._auth_headers()["X-Kiln-Client-Version"] == "9.9.9"


def test_status_end_to_end_tells_an_operator_their_daemon_is_stale(
    tmp_path, monkeypatch
):
    """Through the real files and the real comparison: a state file written by
    an old daemon, a newer package on disk, and the operator finds out."""
    monkeypatch.setattr(bc, "_STATE_FILE", str(tmp_path / "bridge.state"))
    monkeypatch.setattr(bc, "_running_version", lambda: "1.2.0")
    bc.write_bridge_state(connected=True)

    monkeypatch.setattr(bcmd, "_read_license", lambda: "lic")
    monkeypatch.setattr(bcmd, "_service_installed", lambda: False)
    monkeypatch.setattr(bcmd, "_running_pid", lambda: 4242)
    monkeypatch.setattr(bcmd, "_running_supervisor_pid", lambda: None)
    monkeypatch.setattr(bcmd, "_installed_version", lambda: "1.3.2")
    # No network in a test, and none needed for this half.
    monkeypatch.setattr(bcmd, "_latest_published_version", lambda: None)

    out = CliRunner().invoke(bridge, ["status"])
    assert out.exit_code == 0
    assert "on, connected" in out.output
    assert "1.2.0" in out.output and "1.3.2" in out.output
    assert "kiln bridge restart" in out.output


def test_status_survives_a_pypi_check_that_explodes(tmp_path, monkeypatch):
    """A version nudge must never be able to break the diagnostic somebody
    reaches for when their printer has stopped responding."""
    monkeypatch.setattr(bc, "_STATE_FILE", str(tmp_path / "bridge.state"))
    bc.write_bridge_state(connected=True)

    def boom():
        raise RuntimeError("PyPI is on fire")

    monkeypatch.setattr(bcmd, "_read_license", lambda: "lic")
    monkeypatch.setattr(bcmd, "_service_installed", lambda: False)
    monkeypatch.setattr(bcmd, "_running_pid", lambda: 4242)
    monkeypatch.setattr(bcmd, "_running_supervisor_pid", lambda: None)
    monkeypatch.setattr("kiln.version_check.check_for_update", boom)

    out = CliRunner().invoke(bridge, ["status"])
    assert out.exit_code == 0
    assert "on, connected" in out.output


# --- `kiln bridge restart`: one verb over the two-command pairs -------------
#
# Which pair worked depended on how the bridge was supervised, and the wrong
# one does nothing at all.  The axis that matters is not "is a service
# installed" but "who brings a dead bridge back": launchd and systemd do, the
# Windows Run key does not, and a session `start` is watched by our own
# supervisor.  These walk all three.


def _arm_restart(
    monkeypatch,
    *,
    running_pid,
    supervisor_pid,
    service_installed,
    os_supervises,
    connected_after=False,
    version_after=None,
):
    """Stand in for every OS interaction `restart` can reach, and record it."""
    seen: dict[str, list] = {"killed": [], "stopped": [], "spawned": [], "installed": []}
    monkeypatch.setattr(bcmd, "_running_pid", lambda: running_pid)
    monkeypatch.setattr(bcmd, "_running_supervisor_pid", lambda: supervisor_pid)
    monkeypatch.setattr(bcmd, "_service_installed", lambda: service_installed)
    monkeypatch.setattr(bcmd, "_os_supervises_the_bridge", lambda: os_supervises)
    monkeypatch.setattr(bcmd, "_preflight", lambda: None)
    monkeypatch.setattr(bcmd, "_terminate", seen["killed"].append)
    monkeypatch.setattr(bcmd, "_stop_process", lambda: seen["stopped"].append(1) or True)
    monkeypatch.setattr(
        bcmd, "_spawn_supervised_bridge", lambda: seen["spawned"].append(1) or 77
    )
    monkeypatch.setattr(
        bcmd, "_install_service", lambda: seen["installed"].append(1) or (True, "")
    )
    monkeypatch.setattr(bcmd, "_await_connected", lambda timeout=0: connected_after)
    monkeypatch.setattr(
        bcmd, "read_bridge_state",
        lambda: {"version": version_after} if version_after else {},
    )
    return seen


def test_restart_under_launchd_ends_the_process_and_lets_the_os_respawn(monkeypatch):
    """Verified by execution on macOS: a KeepAlive job SIGTERM'd at the
    process level is respawned by launchd, in about a second once its throttle
    interval has passed.  Deliberately NOT a service reinstall — on Linux
    `systemctl enable --now` only STARTS an inactive unit, so a running one
    would keep its old code and the restart would silently do nothing.
    """
    seen = _arm_restart(
        monkeypatch, running_pid=333, supervisor_pid=None,
        service_installed=True, os_supervises=True,
    )
    out = CliRunner().invoke(bridge, ["restart"])
    assert out.exit_code == 0, out.output
    assert seen["killed"] == [333], "did not end the process launchd would replace"
    assert seen["installed"] == [], "rewrote the service definition instead"
    assert seen["stopped"] == [], "killed the supervisor launchd is standing in for"


def test_restart_on_windows_cycles_our_own_supervisor(monkeypatch):
    """The Run key launches at login and never looks again, so an installed
    service there is NOT a supervisor: killing the bridge and waiting would
    leave nothing to bring it back."""
    seen = _arm_restart(
        monkeypatch, running_pid=333, supervisor_pid=444,
        service_installed=True, os_supervises=False,
    )
    out = CliRunner().invoke(bridge, ["restart"])
    assert out.exit_code == 0, out.output
    assert seen["stopped"] == [1] and seen["spawned"] == [1]
    assert seen["installed"] == [], "reinstalled the Run key to restart a process"


def test_restart_of_a_session_bridge_stops_and_respawns(monkeypatch):
    """A deliberate restart is not a crash, so it goes through a clean stop
    rather than killing the child and spending the supervisor's crash-loop
    budget, which exists to notice a genuinely broken install."""
    seen = _arm_restart(
        monkeypatch, running_pid=333, supervisor_pid=444,
        service_installed=False, os_supervises=False,
    )
    out = CliRunner().invoke(bridge, ["restart"])
    assert out.exit_code == 0, out.output
    assert seen["stopped"] == [1] and seen["spawned"] == [1]
    assert seen["killed"] == [], "killed the child out from under its supervisor"


def test_restart_starts_an_enabled_bridge_that_is_not_running(monkeypatch):
    """The dead end this fixes: status used to send this exact state to
    `kiln bridge start`, which sees a login service and returns without
    starting anything."""
    seen = _arm_restart(
        monkeypatch, running_pid=None, supervisor_pid=None,
        service_installed=True, os_supervises=True,
    )
    out = CliRunner().invoke(bridge, ["restart"])
    assert out.exit_code == 0, out.output
    assert seen["installed"] == [1], "left an enabled-but-dead bridge dead"


def test_restart_says_so_when_there_is_nothing_to_restart(monkeypatch):
    seen = _arm_restart(
        monkeypatch, running_pid=None, supervisor_pid=None,
        service_installed=False, os_supervises=False,
    )
    out = CliRunner().invoke(bridge, ["restart"])
    assert out.exit_code == 0, out.output
    assert "nothing to restart" in out.output
    assert "kiln bridge start" in out.output
    assert seen["spawned"] == [] and seen["installed"] == []


def test_restart_reports_the_version_it_came_back_as(monkeypatch):
    """Closes the loop on the fact that motivated the restart."""
    _arm_restart(
        monkeypatch, running_pid=333, supervisor_pid=None,
        service_installed=True, os_supervises=True,
        connected_after=True, version_after="1.3.2",
    )
    out = CliRunner().invoke(bridge, ["restart"])
    assert "now running Kiln 1.3.2" in out.output
    # A restart cycles a process; the machine owns the job and keeps going.
    assert "keeps printing" in out.output


def test_restart_refuses_rather_than_killing_a_working_bridge(monkeypatch):
    """If the environment can no longer run a bridge, restarting into it
    would trade a working stale bridge for no bridge at all."""
    seen = _arm_restart(
        monkeypatch, running_pid=333, supervisor_pid=None,
        service_installed=True, os_supervises=True,
    )

    def refuse():
        raise bcmd.click.ClickException("Sign in first")

    monkeypatch.setattr(bcmd, "_preflight", refuse)
    out = CliRunner().invoke(bridge, ["restart"])
    assert out.exit_code != 0
    assert seen["killed"] == [], "killed a working bridge it could not bring back"


def test_the_status_hint_for_an_enabled_dead_bridge_points_at_a_command_that_works():
    """`kiln bridge start` refuses in exactly this state.  The advice read as
    help and did nothing, which is the same class of defect as the version
    advice that used to branch."""
    _, lines = _describe_status(
        signed_in=True, enabled=True, running=False, connected=False,
        since=None, now=1000.0,
    )
    hint = next(ln for ln in lines if "Start it now" in ln)
    assert "kiln bridge restart" in hint
    assert "kiln bridge start " not in hint


def test_the_gave_up_hint_points_at_a_command_that_works_under_a_login_service():
    """The same dead end as the test above, one branch over, and it outlived
    the fix to that one.

    Reachable on Windows, where `enable` installs a Run key AND runs our own
    supervisor — so the supervisor can give up while the login service is still
    installed.  `kiln bridge start` refuses in exactly that state, so telling
    the operator to "start it again" left them with a bridge still down and
    nothing to try.
    """
    _, lines = _describe_status(
        signed_in=True, enabled=True, running=False, connected=False,
        since=None, now=1000.0, gave_up=True,
    )
    hint = next(ln for ln in lines if "Fix that" in ln)
    assert "kiln bridge restart" in hint
    assert "kiln bridge start " not in hint


def test_the_gave_up_hint_still_says_start_when_there_is_no_login_service():
    """The other half of the same branch, so the fix above is a fork and not a
    swap: a session `start` that gave up has no service installed, `start` is
    not refused, and it is the command that works."""
    _, lines = _describe_status(
        signed_in=True, enabled=False, running=False, connected=False,
        since=None, now=1000.0, gave_up=True,
    )
    hint = next(ln for ln in lines if "Fix that" in ln)
    assert "kiln bridge start" in hint
    assert "restart" not in hint


def test_start_does_not_reassure_over_a_managed_bridge_that_is_down(monkeypatch):
    """The other door onto the same state.  `start` is the command someone
    types precisely because nothing is running, and over a down bridge
    "it's managed for you" reads as an all-clear — the same advice-that-does-
    nothing defect, met by typing rather than by reading status.

    It must still refuse to spawn: two supervisors over one child is why this
    branch exists.  Only the words change.
    """
    monkeypatch.setattr(bcmd, "_read_license", lambda: "lic")
    monkeypatch.setattr(bcmd, "_service_installed", lambda: True)
    monkeypatch.setattr(bcmd, "_running_pid", lambda: None)
    monkeypatch.setattr(bcmd, "_running_supervisor_pid", lambda: None)
    spawned: list[int] = []
    monkeypatch.setattr(bcmd, "_spawn_supervised_bridge", lambda: spawned.append(1) or 9)

    out = CliRunner().invoke(bridge, ["start"])

    assert out.exit_code == 0
    assert spawned == [], "stacked a second supervisor under the login service"
    assert "managed for you" not in out.output
    assert "kiln bridge restart" in out.output


def test_start_still_says_it_is_managed_when_the_managed_bridge_is_up(monkeypatch):
    """The fork above must not cost the honest message in the state that
    always was honest: service installed AND running really is managed."""
    monkeypatch.setattr(bcmd, "_read_license", lambda: "lic")
    monkeypatch.setattr(bcmd, "_service_installed", lambda: True)
    monkeypatch.setattr(bcmd, "_running_pid", lambda: 4242)
    monkeypatch.setattr(bcmd, "_running_supervisor_pid", lambda: None)
    spawned: list[int] = []
    monkeypatch.setattr(bcmd, "_spawn_supervised_bridge", lambda: spawned.append(1) or 9)

    out = CliRunner().invoke(bridge, ["start"])

    assert out.exit_code == 0
    assert spawned == []
    assert "managed for you" in out.output


def test_both_doors_onto_a_down_managed_bridge_name_the_same_verb(monkeypatch):
    """`status` and `start` reach this state independently and each names the
    recovery command in its own string.  Pin that they agree, so a later rename
    of the verb cannot fix one door and leave the other advising a command that
    does nothing — which is the defect this whole cluster is about.
    """
    monkeypatch.setattr(bcmd, "_read_license", lambda: "lic")
    monkeypatch.setattr(bcmd, "_service_installed", lambda: True)
    monkeypatch.setattr(bcmd, "_running_pid", lambda: None)
    monkeypatch.setattr(bcmd, "_running_supervisor_pid", lambda: None)
    monkeypatch.setattr(bcmd, "_spawn_supervised_bridge", lambda: 9)
    from_start = CliRunner().invoke(bridge, ["start"]).output

    verb = "kiln bridge restart"
    for enabled_state in (dict(gave_up=True), dict(gave_up=False)):
        _, lines = _describe_status(
            signed_in=True, enabled=True, running=False, connected=False,
            since=None, now=1000.0, **enabled_state,
        )
        assert any(verb in ln for ln in lines), enabled_state
    assert verb in from_start


def test_which_platforms_have_a_supervising_login_service(monkeypatch):
    """The axis `restart` turns on, tested directly rather than only stubbed.

    Verified by execution on macOS (a KeepAlive LaunchAgent respawns a
    SIGTERM'd child in about a second); by documented behaviour on Linux
    (`Restart=always` covers a process killed by a signal, and only an
    explicit `systemctl stop` suppresses it) and on Windows (a Run key runs
    once at login and watches nothing, which is why `enable` starts our own
    supervisor there).
    """
    import sys as _sys

    monkeypatch.setattr(bcmd, "_service_installed", lambda: True)
    for platform, supervises in (
        ("darwin", True), ("linux", True), ("win32", False),
    ):
        monkeypatch.setattr(_sys, "platform", platform)
        assert bcmd._os_supervises_the_bridge() is supervises, platform

    # No service installed means nobody is watching, on any platform.
    monkeypatch.setattr(bcmd, "_service_installed", lambda: False)
    for platform in ("darwin", "linux", "win32"):
        monkeypatch.setattr(_sys, "platform", platform)
        assert bcmd._os_supervises_the_bridge() is False, platform


# --- first-run onboarding (enable = the paste's last line) ------------------


from kiln.cli.bridge_commands import (  # noqa: E402
    _credential_prompts,
    _discovered_host,
    _discovered_line,
    _offer_first_printer,
    _slicer_note,
    _suggest_printer_name,
)
from kiln.discovery import DiscoveredPrinter  # noqa: E402


def _found(**kw) -> DiscoveredPrinter:
    base = dict(host="192.168.1.23", port=7125, printer_type="moonraker", name="Creality K1C")
    base.update(kw)
    return DiscoveredPrinter(**base)


def test_suggest_printer_name_slugs_the_advertised_name():
    assert _suggest_printer_name("Creality K1C", "moonraker") == "creality-k1c"
    assert _suggest_printer_name("", "moonraker") == "moonraker"
    assert _suggest_printer_name("...", "octoprint") == "octoprint"


def test_discovered_host_keeps_real_ports_and_bares_bambu():
    assert _discovered_host(_found()) == "192.168.1.23:7125"
    assert _discovered_host(_found(port=80)) == "192.168.1.23"
    # Bambu speaks MQTT on its own fixed port — whatever port discovery saw,
    # the saved host must stay bare or the adapter would dial the wrong thing.
    assert _discovered_host(_found(printer_type="bambu", port=990)) == "192.168.1.23"


def test_discovered_line_prefers_the_advertised_name():
    assert _discovered_line(_found()) == "Creality K1C — 192.168.1.23:7125 (moonraker)"
    assert _discovered_line(_found(name="", port=80)) == "moonraker — 192.168.1.23 (moonraker)"


def test_onboarding_skips_when_a_printer_already_exists(monkeypatch):
    import kiln.cli.config as cfg
    import kiln.cli.discovery as disc

    monkeypatch.setattr(cfg, "list_printers", lambda **kw: [{"name": "k1c"}])
    monkeypatch.setattr(
        disc, "discover_printers", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("scanned"))
    )
    _offer_first_printer()  # must not scan, prompt, or raise


def test_onboarding_skips_without_a_tty(monkeypatch):
    import kiln.cli.config as cfg
    import kiln.cli.discovery as disc

    monkeypatch.setattr(cfg, "list_printers", lambda **kw: [])
    monkeypatch.setattr(bcmd, "_onboarding_interactive", lambda: False)
    monkeypatch.setattr(
        disc, "discover_printers", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("scanned"))
    )
    _offer_first_printer()  # windowless deep-link relaunch: quiet no-op


def test_onboarding_saves_the_one_found_printer(monkeypatch, capsys):
    import click as _click

    import kiln.cli.config as cfg
    import kiln.cli.discovery as disc
    import kiln.cli.printer_model_prompt as pmp

    saved = {}

    def fake_save(name, ptype, host, **kw):
        saved.update({"name": name, "type": ptype, "host": host, **kw})
        return "/tmp/config.yaml"

    monkeypatch.setattr(cfg, "list_printers", lambda **kw: [])
    monkeypatch.setattr(cfg, "save_printer", fake_save)
    monkeypatch.setattr(bcmd, "_onboarding_interactive", lambda: True)
    monkeypatch.setattr(disc, "discover_printers", lambda *a, **kw: [_found()])
    monkeypatch.setattr(pmp, "prompt_for_printer_model", lambda *a, **kw: "k1c")
    monkeypatch.setattr(_click, "confirm", lambda *a, **kw: True)

    _offer_first_printer()

    assert saved == {
        "name": "creality-k1c",
        "type": "moonraker",
        "host": "192.168.1.23:7125",
        "printer_model": "k1c",
    }
    assert "Saved ✓" in capsys.readouterr().out


def test_onboarding_empty_scan_points_at_the_manual_path(monkeypatch, capsys):
    import kiln.cli.config as cfg
    import kiln.cli.discovery as disc

    monkeypatch.setattr(cfg, "list_printers", lambda **kw: [])
    monkeypatch.setattr(bcmd, "_onboarding_interactive", lambda: True)
    monkeypatch.setattr(disc, "discover_printers", lambda *a, **kw: [])

    _offer_first_printer()

    out = capsys.readouterr().out
    assert "kiln auth" in out  # the manual door is named, not implied
    assert "Turning the bridge on anyway" in out  # never a dead end


def test_credential_prompts_ask_only_what_the_backend_needs(monkeypatch):
    import click as _click

    # Moonraker/Creality: nothing to ask.
    assert _credential_prompts("moonraker", "") == {}
    assert _credential_prompts("creality", "") == {}

    # Bambu: access code + serial (discovery's serial offered as the default).
    answers = iter(["12345678", "0309CA123456789"])
    monkeypatch.setattr(_click, "prompt", lambda *a, **kw: next(answers))
    assert _credential_prompts("bambu", "0309CA123456789") == {
        "access_code": "12345678",
        "serial": "0309CA123456789",
    }


def test_slicer_note_speaks_only_when_the_slicer_is_missing(monkeypatch, capsys):
    import kiln.slicer as slicer_mod

    def missing(*a, **kw):
        raise slicer_mod.SlicerNotFoundError("none")

    monkeypatch.setattr(slicer_mod, "find_slicer", missing)
    _slicer_note()
    out = capsys.readouterr().out
    assert "printing needs a slicer" in out
    assert "Windows" in out  # the hint covers every platform the paste targets

    monkeypatch.setattr(slicer_mod, "find_slicer", lambda *a, **kw: object())
    _slicer_note()
    assert capsys.readouterr().out == ""


def test_enable_runs_onboarding_before_the_service_and_notes_the_slicer(monkeypatch):
    calls = []
    monkeypatch.setattr(bcmd, "_preflight", lambda: calls.append("preflight"))
    monkeypatch.setattr(bcmd, "_offer_first_printer", lambda: calls.append("printer"))
    monkeypatch.setattr(bcmd, "_stop_process", lambda: calls.append("stop"))
    monkeypatch.setattr(bcmd, "_install_service", lambda: (calls.append("service"), (True, ""))[1])
    monkeypatch.setattr(bcmd, "_await_connected", lambda: True)
    monkeypatch.setattr(bcmd, "_slicer_note", lambda: calls.append("slicer"))
    monkeypatch.setattr(bcmd.sys, "platform", "darwin")

    result = CliRunner().invoke(bridge, ["enable"])

    assert result.exit_code == 0, result.output
    assert calls == ["preflight", "printer", "stop", "service", "slicer"]
