"""A printer one Kiln session holds is not commanded by another.

Until 2026-11-01 ``acquire_printer_lock`` and ``release_printer_lock`` called
a lock manager that existed in neither repo, so both failed on every install;
their tests patched the missing name into existence (``create=True``) and
stayed green.  These tests use no stand-in for the hold: a real second
process takes it, a real gated printer is refused through the doors a person
meets, and the hold ends when that process does.

Pinned beside the rule, as for the one-printer rule: what a hold never
stops (looking, an emergency stop) and that every uncertainty allows.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

import kiln.server as srv
from kiln import printer_lock
from kiln.printers import engagement
from kiln.printers.base import PrinterCapabilities, PrinterLockedError, PrinterStatus
from kiln.registry import PrinterRegistry
from tests.test_refusals_speak_for_themselves import _Printer, _tool

_SRC = str(Path(printer_lock.__file__).resolve().parents[1])


@pytest.fixture
def shop(monkeypatch, tmp_path):
    """voron and ender registered; this test's own ``~/.kiln``."""
    monkeypatch.setenv("KILN_HOME", str(tmp_path / "kiln-home"))
    voron = _Printer("AAA111", PrinterStatus.PRINTING, layer=12)
    ender = _Printer("BBB222", file=None)
    voron._capabilities = ender._capabilities = PrinterCapabilities(
        can_upload=True, can_set_temp=True, can_send_gcode=True, can_pause=True,
    )
    registry = PrinterRegistry()
    registry.register("voron", voron)
    registry.register("ender", ender)
    monkeypatch.setattr("kiln.registry.get_registry", lambda: registry)
    monkeypatch.setattr(srv, "_get_registry", lambda: registry)
    monkeypatch.setattr(srv, "_adapter", voron)
    monkeypatch.setattr(srv, "_check_auth", lambda *_a, **_k: None)
    # The one-printer rule is not under test here: lift it.
    monkeypatch.setattr(engagement, "_multi_machine_tier", lambda: True)
    monkeypatch.setattr(srv, "_tool_limiter", srv._ToolRateLimiter())
    return voron, ender


@pytest.fixture
def other_session(tmp_path):
    """A second Kiln process that takes a hold on voron and stays alive
    until the test ends it.  Yields the process."""
    procs: list[subprocess.Popen] = []

    def _start(*, holder="claude-desktop", minutes=30.0, home=None):
        script = textwrap.dedent(
            f"""
            import sys, time
            sys.path.insert(0, {_SRC!r})
            from kiln import printer_lock

            class Voron:
                name = "moonraker"
                serial = "AAA111"
                host = ""

            print(printer_lock.acquire(Voron(), "voron", holder={holder!r}, hold_minutes={minutes!r})["success"], flush=True)
            time.sleep(120)
            """
        )
        env = {**os.environ, "KILN_HOME": str(home or (tmp_path / "kiln-home"))}
        proc = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True, env=env)
        procs.append(proc)
        assert proc.stdout.readline().strip() == "True"
        return proc

    yield _start
    for proc in procs:
        proc.kill()
        proc.wait()


def _door(name):
    """A registered tool, as the MCP dispatch calls it."""
    return srv.mcp._tool_manager._tools[name].fn


def _end(proc):
    proc.kill()
    proc.wait()


class TestAnotherSessionsHoldIsHonoured:
    def test_a_command_is_refused_through_the_door_a_person_uses(self, shop, other_session):
        other_session()
        answer = _tool(srv.pause_print)(printer_name="voron")
        assert answer["success"] is False
        assert answer["error"]["code"] == "PRINTER_LOCKED"
        message = answer["error"]["message"]
        assert message.startswith("voron is held by claude-desktop in another Kiln session")
        assert "release_printer_lock(force=True)" in message
        assert "online" not in message  # never the connection advice

    def test_the_adapter_itself_refuses(self, shop, other_session):
        voron, _ = shop
        other_session()
        with pytest.raises(PrinterLockedError) as refused:
            voron.cancel_print()
        assert refused.value.verdict["held_by"] == "claude-desktop"

    def test_looking_and_an_emergency_stop_always_go_through(self, shop, other_session):
        voron, _ = shop
        other_session()
        assert voron.get_state().state == PrinterStatus.PRINTING
        assert voron.emergency_stop().success is True

    def test_another_printer_is_not_held(self, shop, other_session):
        _, ender = shop
        other_session()
        assert ender.pause_print().success is True

    def test_the_hold_ends_when_its_session_does(self, shop, other_session):
        voron, _ = shop
        proc = other_session()
        with pytest.raises(PrinterLockedError):
            voron.pause_print()
        _end(proc)
        assert voron.pause_print().success is True

    def test_the_hold_ends_when_it_runs_out(self, shop, other_session, monkeypatch):
        voron, _ = shop
        other_session(minutes=1.0)
        later = time.time() + 61
        monkeypatch.setattr(printer_lock.time, "time", lambda: later)
        assert voron.pause_print().success is True


class TestTakingAndEndingAHold:
    def test_this_session_holds_and_keeps_every_command(self, shop):
        voron, _ = shop
        import asyncio

        held = asyncio.run(_door("acquire_printer_lock")(printer_name="voron", holder="codex"))
        assert held["success"] is True and held["locked"] is True
        assert voron.pause_print().success is True
        assert _door("release_printer_lock")(printer_name="voron")["released"] is True

    def test_asking_while_another_session_holds_names_the_holder(self, shop, other_session):
        import asyncio

        other_session()
        answer = asyncio.run(_door("acquire_printer_lock")(printer_name="voron"))
        assert answer["success"] is False and answer["code"] == "PRINTER_LOCKED"
        assert answer["held_by"] == "claude-desktop"

    def test_waiting_takes_the_hold_once_the_other_session_ends(self, shop, other_session):
        import asyncio
        import threading

        proc = other_session()
        threading.Timer(0.8, _end, args=(proc,)).start()
        answer = asyncio.run(_door("acquire_printer_lock")(printer_name="voron", timeout_seconds=10))
        assert answer["success"] is True

    def test_another_sessions_hold_is_released_only_with_force(self, shop, other_session):
        voron, _ = shop
        other_session()
        refused = _door("release_printer_lock")(printer_name="voron")
        assert refused["success"] is False and refused["held_by"] == "claude-desktop"
        taken = _door("release_printer_lock")(printer_name="voron", force=True)
        assert taken["released"] is True and taken["took_from"] == "claude-desktop"
        assert voron.pause_print().success is True

    def test_releasing_a_printer_nobody_holds_says_so(self, shop):
        answer = _door("release_printer_lock")(printer_name="voron")
        assert answer == {
            "success": True, "printer": "voron", "released": False,
            "note": "voron was not held by any Kiln session.",
        }

    def test_an_unknown_printer_is_the_registrys_answer(self, shop):
        import asyncio

        answer = asyncio.run(_door("acquire_printer_lock")(printer_name="nope"))
        assert answer["success"] is False


class TestEveryUncertaintyAllows:
    def test_an_unreadable_record_holds_nothing(self, shop):
        voron, _ = shop
        printer_lock._store_path().write_text("{not json")
        assert voron.pause_print().success is True

    def test_a_record_from_another_version_holds_nothing(self, shop):
        voron, _ = shop
        printer_lock._store_path().write_text(json.dumps({"version": 99, "holds": {}}))
        assert voron.pause_print().success is True

    def test_a_dead_holder_holds_nothing(self, shop):
        voron, _ = shop
        gone = subprocess.Popen([sys.executable, "-c", "pass"])
        gone.wait()
        hold = {
            "label": "voron", "holder": "gone", "session": "x" * 32, "pid": gone.pid,
            "acquired_at": time.time(), "expires_at": time.time() + 600,
        }
        printer_lock._write({printer_lock.machine_key(voron): hold})
        assert voron.pause_print().success is True

    def test_windows_never_signals_the_holder(self, monkeypatch):
        """On Windows a signal 0 is TerminateProcess: asking would kill it."""
        monkeypatch.setattr(printer_lock.sys, "platform", "win32")
        monkeypatch.setattr(printer_lock.os, "kill", lambda *_a: pytest.fail("signalled the holder"))
        assert printer_lock._process_alive(os.getpid() + 1) is True
