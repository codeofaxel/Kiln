"""A printer one Kiln session holds is not commanded by another.

A test that patches a missing name into existence (``create=True``) stays
green over a lock manager that exists nowhere.  These tests use no stand-in
for the hold: a real second process takes it, a real gated printer is
refused through the doors a person meets, and the hold ends when that
process does.

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
from kiln.printers.base import (
    PrinterCapabilities,
    PrinterEngagementError,
    PrinterLockedError,
    PrinterStatus,
)
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
        assert answer["error"]["message"] == (
            "Voron is reserved by claude-desktop, another Kiln session on this computer, "
            "for up to 30 more minutes, so Kiln didn't send this command. You can still "
            "check its status and camera, and an emergency stop always goes through. To "
            "use it now, ask that session to let it go, or take it over."
        )
        # How an agent takes it over rides beside the words, never in them.
        assert "force=true" in answer["refusal"]["agent_hint"]

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
        assert held["message"] == (
            "Voron is reserved for this session for 30 minutes. Other Kiln sessions on "
            "this computer can still check its status and camera and send an emergency "
            "stop, but nothing else, until you release it or the time runs out."
        )
        assert voron.pause_print().success is True
        assert _door("release_printer_lock")(printer_name="voron")["released"] is True

    def test_asking_while_another_session_holds_names_the_holder(self, shop, other_session):
        import asyncio

        other_session()
        answer = asyncio.run(_door("acquire_printer_lock")(printer_name="voron"))
        assert answer["success"] is False and answer["error"]["code"] == "PRINTER_LOCKED"
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
        assert refused["error"]["message"] == (
            "Voron is reserved by claude-desktop, another Kiln session on this computer, "
            "and only that session can let it go. To take it over anyway, release it with force."
        )
        taken = _door("release_printer_lock")(printer_name="voron", force=True)
        assert taken["released"] is True and taken["took_from"] == "claude-desktop"
        assert voron.pause_print().success is True

    def test_releasing_a_printer_nobody_holds_says_so(self, shop):
        answer = _door("release_printer_lock")(printer_name="voron")
        assert answer == {
            "success": True, "printer": "voron", "released": False,
            "message": "Voron wasn't reserved by any Kiln session.",
        }

    def test_an_unknown_printer_is_the_registrys_answer(self, shop):
        import asyncio

        answer = asyncio.run(_door("acquire_printer_lock")(printer_name="nope"))
        assert answer["success"] is False


class TestTheOnePrinterRuleSpeaksFirst:
    """Below Kiln Business, Kiln works with one printer at a time.  A
    reservation sits inside that rule: on a second printer the rule refuses,
    in its own words with its one line about Business, and the reservation
    stays silent; and nobody reserves a printer the rule would not let them
    use.  Wherever the reservation does speak, what it says about looking and
    an emergency stop is true."""

    @pytest.fixture
    def driving_voron(self, shop, monkeypatch):
        voron, ender = shop
        monkeypatch.setattr(engagement, "_multi_machine_tier", lambda: False)
        engagement.engage(voron, voron.get_job(), reason="started", label="voron")
        return voron, ender

    def test_a_second_printer_gets_the_one_printer_refusal_not_the_reservation(
        self, driving_voron, other_session,
    ):
        _, ender = driving_voron
        _hold(ender, "ender", holder="codex")
        answer = _tool(srv.pause_print)(printer_name="ender")
        assert answer["error"]["code"] == "TIER_SINGLE_PRINTER_LIMIT"
        message = answer["error"]["message"]
        assert "reserved" not in message
        assert message.lower().count("business") == 1

    def test_an_emergency_stop_on_a_second_printer_is_still_the_paid_line(self, driving_voron):
        _, ender = driving_voron
        _hold(ender, "ender", holder="codex")
        with pytest.raises(PrinterEngagementError) as refused:
            ender.emergency_stop()
        assert refused.value.verdict["code"] == "TIER_SINGLE_PRINTER_LIMIT"

    def test_on_the_printer_kiln_drives_the_reservation_speaks_and_a_stop_goes_through(
        self, driving_voron, other_session,
    ):
        voron, _ = driving_voron
        other_session()
        with pytest.raises(PrinterLockedError):
            voron.pause_print()
        assert voron.emergency_stop().success is True

    def test_a_second_printer_cannot_be_reserved_below_business(self, driving_voron):
        import asyncio

        answer = asyncio.run(_door("acquire_printer_lock")(printer_name="ender"))
        assert answer["error"]["code"] == "TIER_SINGLE_PRINTER_LIMIT"
        assert printer_lock._read() == {}

    def test_a_handed_back_print_keeps_its_return_while_another_session_has_it(self, driving_voron):
        voron, ender = driving_voron
        ender.status, ender.file, ender.layer = PrinterStatus.PRINTING, "gasket.gcode", 3
        engagement.hand_back(voron)
        with engagement.internal_read():
            engagement.engage(ender, ender.get_job(), reason="started", label="ender")
        engagement.hand_back(ender)
        with engagement.internal_read():
            engagement.engage(voron, voron.get_job(), reason="started", label="voron")
        # The one-printer rule would let this command through on ender's one
        # return; the reservation refuses it, and the return stays unspent.
        assert engagement.refusal_ahead(ender, "pause_print") is None
        _hold(ender, "ender", holder="codex")
        with pytest.raises(PrinterLockedError):
            ender.pause_print()
        handbacks = engagement._read_store().get("handbacks") or {}
        assert handbacks[engagement.machine_id(ender)].get("returns_used", 0) == 0


def _hold(adapter, label, *, holder):
    """Another live session's reservation, written as one would write it."""
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    _SLEEPERS.append(sleeper)
    printer_lock._write({printer_lock.machine_key(adapter, label): {
        "label": label, "holder": holder, "session": "s" * 32, "pid": sleeper.pid,
        "acquired_at": time.time(), "expires_at": time.time() + 600,
    }})


_SLEEPERS: list[subprocess.Popen] = []


@pytest.fixture(autouse=True)
def _end_sleepers():
    yield
    while _SLEEPERS:
        _end(_SLEEPERS.pop())


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
        """On Windows a signal 0 is TerminateProcess: asking would kill it.

        The holder is a live child, so the answer must be ``True`` and must
        come without a signal.  Off Windows the platform is faked and the
        query-only probe stubbed, since there is no handle to open."""
        import kiln.proc_alive as proc_alive

        holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        _SLEEPERS.append(holder)
        # Patched for the question only.  ``os`` is the one shared module, and
        # the cleanup that ends the holder signals it through ``os.kill`` on
        # POSIX; a patch still standing then fails the run after it passed.
        with monkeypatch.context() as m:
            m.setattr(printer_lock.os, "kill", lambda *_a: pytest.fail("signalled the holder"))
            if os.name != "nt":
                m.setattr(proc_alive.os, "name", "nt")
                m.setattr(proc_alive, "_windows_alive", lambda pid, unknown: True)
            assert printer_lock._process_alive(holder.pid) is True
