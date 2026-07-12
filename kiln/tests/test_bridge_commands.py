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
    assert cmd == '"C:\\Py 3.12\\pythonw.exe" -m kiln.bridge_client'


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
    monkeypatch.setattr(bcmd, "_spawn_bridge", lambda: spawned.append(1) or 999)
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
        "status", "start", "stop", "enable", "disable", "handle-uri",
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
