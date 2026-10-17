"""Serial adapter: a cancel that tells the truth, and a printer that waits for a click.

Two ways the USB adapter used to mislead, both from Marlin's own replies:

1. A command the firmware does not know is answered with an ``echo:Unknown
   command: "M524"`` line and then a plain ``ok``.  The adapter took the
   ``ok`` for success, so the old M0 fallback never ran and the print kept
   going while Kiln said it was cancelled.  M0 is not a cancel in any case.
2. A printer parked at an M0 in the file does not read its command queue, so
   the status poll got no ``ok`` and the printer read as offline.  It does
   print ``echo:busy: paused for user`` every few seconds meanwhile.

The fake port below is scripted per command and timed in real seconds, so the
"does not wait for the timeout" and "keeps waiting while the printer says it
is busy" cases are measured rather than assumed.
"""

from __future__ import annotations

import sys
import time
from types import ModuleType
from unittest.mock import patch

import pytest

import kiln.printers.base as base
from kiln.printers.base import PrinterError, PrinterStatus

UNKNOWN_CANCEL = ['echo:Unknown command: "M524"', "ok"]
BUSY_USER = "echo:busy: paused for user"


class _FakeSerialException(Exception):
    pass


class _ScriptedPort:
    """A serial port whose replies depend on the command written to it.

    ``script`` maps a command word (``"M105"``) to a list of replies; a reply
    is a line, or ``(delay_seconds, line)`` for one that arrives later.  A
    callable may be given instead to compute the list from the full command.
    Anything unscripted is answered ``ok``.
    """

    def __init__(self, script: dict, timeout: float = 10) -> None:
        self.script = script
        self.timeout = timeout
        self.is_open = True
        self.sent: list[str] = []
        self._pending: list[tuple[float, str]] = []

    # -- pyserial surface the adapter uses ---------------------------------
    def reset_input_buffer(self) -> None:
        self._pending.clear()

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.is_open = False

    def write(self, data: bytes) -> None:
        command = data.decode().strip()
        self.sent.append(command)
        word = command.split()[0]
        reply = self.script.get(word, ["ok"])
        if callable(reply):
            reply = reply(command)
        now = time.monotonic()
        for item in reply:
            delay, line = item if isinstance(item, tuple) else (0.0, item)
            self._pending.append((now + delay, line))
        self._pending.sort(key=lambda p: p[0])

    def readline(self) -> bytes:
        if self._pending and self._pending[0][0] <= time.monotonic():
            return (self._pending.pop(0)[1] + "\n").encode()
        time.sleep(0.01)
        return b""

    # -- test surface ------------------------------------------------------
    @property
    def unread(self) -> list[str]:
        return [line for _, line in self._pending]


@pytest.fixture(autouse=True)
def _fake_serial_module():
    mod = ModuleType("serial")
    mod.SerialException = _FakeSerialException  # type: ignore[attr-defined]
    old = sys.modules.get("serial")
    sys.modules["serial"] = mod
    yield mod
    if old is not None:
        sys.modules["serial"] = old
    else:
        sys.modules.pop("serial", None)


def _adapter(script: dict, *, timeout: float = 10):
    from kiln.printers.serial_adapter import SerialPrinterAdapter

    port = _ScriptedPort(script, timeout=timeout)
    sys.modules["serial"].Serial = lambda **_kw: port  # type: ignore[attr-defined]
    with (
        patch.object(SerialPrinterAdapter, "_wait_for_startup"),
        patch.object(SerialPrinterAdapter, "_capture_machine_type"),
    ):
        adapter = SerialPrinterAdapter("/dev/ttyUSB0", timeout=timeout, printer_name="t")
    return adapter, port


M105_OK = ["ok T:210.0 /210.0 B:60.0 /60.0"]
PRINTING = ["SD printing byte 500/1000", "ok"]


# ---------------------------------------------------------------------------
# An unknown command is not a success
# ---------------------------------------------------------------------------


class TestUnknownCommand:
    def test_unknown_command_reply_raises_and_consumes_the_ok(self):
        adapter, port = _adapter({"M524": UNKNOWN_CANCEL})
        with pytest.raises(base.UnsupportedCommand) as caught:
            adapter._send_command("M524")
        assert caught.value.command == "M524"
        assert isinstance(caught.value, PrinterError)
        # The ok that follows the echo line was read, not left in the buffer
        # for the next command to mistake for its own answer.
        assert port.unread == []

    def test_match_is_case_insensitive(self):
        adapter, _ = _adapter({"M524": ['ECHO:UNKNOWN COMMAND: "M524"', "ok"]})
        with pytest.raises(base.UnsupportedCommand):
            adapter._send_command("M524")

    def test_unknown_command_without_the_following_ok_still_raises(self):
        adapter, _ = _adapter({"M524": ['echo:Unknown command: "M524"']}, timeout=0.2)
        with pytest.raises(base.UnsupportedCommand):
            adapter._send_command("M524")

    def test_ordinary_echo_lines_are_not_unknown_commands(self):
        adapter, _ = _adapter({"M503": ["echo:  M92 X80.00 Y80.00", "ok"]})
        assert "M92" in adapter._send_command("M503")


# ---------------------------------------------------------------------------
# cancel_print
# ---------------------------------------------------------------------------

STOP_SENTENCE = (
    "This printer's firmware cannot abort a print over USB. Kiln paused it and "
    "switched the heaters off; press Stop print on the printer's screen to end it."
)


class TestCancelPrint:
    def test_cancel_accepted_is_unchanged(self):
        adapter, port = _adapter({})
        adapter._current_file = "BENCHY.GCO"
        result = adapter.cancel_print()
        assert result.success is True
        assert adapter._current_file is None
        assert port.sent == ["M524"]

    def test_unknown_m524_pauses_and_heaters_off_and_never_sends_m0(self):
        adapter, port = _adapter({"M524": UNKNOWN_CANCEL})
        adapter._current_file = "BENCHY.GCO"
        result = adapter.cancel_print()

        assert port.sent == ["M524", "M25", "M104 S0", "M140 S0"]
        assert not any(c.split()[0] == "M0" for c in port.sent)
        assert result.success is False
        assert result.code == "CANCEL_UNSUPPORTED_PAUSED_INSTEAD"
        assert result.message == STOP_SENTENCE
        # Truthful state: the print is paused, not gone.
        assert adapter._current_file == "BENCHY.GCO"
        assert adapter._paused is True

    def test_if_the_pause_fails_it_says_so_and_claims_nothing_stopped(self):
        adapter, port = _adapter({"M524": UNKNOWN_CANCEL, "M25": ["Error:no"]})
        adapter._current_file = "BENCHY.GCO"
        result = adapter.cancel_print()

        assert result.success is False
        assert result.code == "CANCEL_UNSUPPORTED_NOT_PAUSED"
        text = result.message.lower()
        assert "could not pause" in text and "still be printing" in text
        assert "switched the heaters off" not in text
        # Heaters are left alone: cutting them under a moving print only
        # turns an unstoppable print into a cold-extrusion one.
        assert "M104 S0" not in port.sent and "M140 S0" not in port.sent
        assert adapter._current_file == "BENCHY.GCO"
        assert adapter._paused is False

    def test_paused_but_heaters_would_not_switch_off_is_said(self):
        adapter, port = _adapter({"M524": UNKNOWN_CANCEL, "M104": ["Error:no"]})
        adapter._current_file = "BENCHY.GCO"
        result = adapter.cancel_print()

        assert result.success is False
        assert "paused it" in result.message
        assert "heaters may still be on" in result.message
        assert not any(c.split()[0] == "M0" for c in port.sent)
        assert adapter._paused is True

    def test_other_m524_failures_propagate_and_never_fall_back_to_m0(self):
        adapter, port = _adapter({"M524": ["Error:Printer halted. kill() called!"]})
        adapter._current_file = "BENCHY.GCO"
        with pytest.raises(PrinterError, match="Firmware error"):
            adapter.cancel_print()
        assert port.sent == ["M524"]
        assert adapter._current_file == "BENCHY.GCO"


# ---------------------------------------------------------------------------
# A printer waiting for a click
# ---------------------------------------------------------------------------


def _waiting_script(line: str = BUSY_USER):
    # Keepalive lines every 50 ms, and no ok ever, like a printer at an M0.
    return {"M105": [(0.05 * i, line) for i in range(1, 400)]}


class TestWaitingForUser:
    def test_poll_during_a_user_wait_reads_paused_and_connected_quickly(self):
        adapter, _ = _adapter(_waiting_script(), timeout=5)
        started = time.monotonic()
        state = adapter.get_state()
        elapsed = time.monotonic() - started

        assert state.connected is True
        assert state.state is PrinterStatus.PAUSED
        assert state.cause == "waiting_for_user"
        assert "waiting for a click on the printer" in state.remedy
        assert "the file told it to stop here" in state.remedy
        assert elapsed < 1.0

    def test_paused_for_input_reads_the_same(self):
        adapter, _ = _adapter(_waiting_script("echo:busy: paused for input"), timeout=5)
        state = adapter.get_state()
        assert state.connected is True
        assert state.state is PrinterStatus.PAUSED

    def test_send_command_raises_waiting_for_user(self):
        adapter, _ = _adapter(_waiting_script(), timeout=5)
        with pytest.raises(base.WaitingForUser):
            adapter._send_command("M105")

    def test_busy_processing_extends_the_wait_instead_of_timing_out(self):
        # The ok arrives at 0.6 s, well past the 0.35 s timeout, but a busy
        # line lands every 0.15 s, so the printer is plainly alive.
        slow_move = [(0.15, "echo:busy: processing"), (0.30, "echo:busy: processing"),
                     (0.45, "echo:busy: processing"), (0.60, "ok")]
        adapter, _ = _adapter({"G28": slow_move}, timeout=0.35)
        assert "ok" in adapter._send_command("G28")

    def test_silence_still_times_out(self):
        adapter, _ = _adapter({"G28": []}, timeout=0.2)
        with pytest.raises(PrinterError, match="Timeout"):
            adapter._send_command("G28")

    def test_keepalive_lines_are_not_the_polls_data(self):
        script = {"M105": [(0.0, "echo:busy: processing"), (0.0, *M105_OK)]}
        adapter, _ = _adapter(script, timeout=2)
        text = adapter._send_command("M105")
        assert "busy" not in text.lower()
        assert adapter._parse_temps(text)["tool_actual"] == 210.0

    def test_the_wait_clears_when_the_printer_answers_again(self):
        adapter, port = _adapter(_waiting_script(), timeout=5)
        assert adapter.get_state().cause == "waiting_for_user"
        port.script["M105"] = M105_OK
        port.script["M27"] = PRINTING
        state = adapter.get_state()
        assert state.cause is None and state.remedy is None
        assert state.state is PrinterStatus.PRINTING


class TestResumeDuringUserWait:
    def _waiting_adapter(self):
        adapter, port = _adapter(_waiting_script(), timeout=5)
        adapter.get_state()  # the poll that sees the wait
        return adapter, port

    def test_resume_is_refused_with_the_knob_sentence_and_no_m24(self):
        adapter, port = self._waiting_adapter()
        result = adapter.resume_print()

        assert result.success is False
        assert result.code == "WAITING_FOR_CLICK_ON_PRINTER"
        assert "cannot release this wait over USB" in result.message
        assert "knob or Resume on the printer" in result.message
        assert "M24" not in port.sent

    def test_force_does_not_bypass_it(self):
        adapter, port = self._waiting_adapter()
        result = adapter.resume_print(force=True)
        assert result.success is False
        assert "M24" not in port.sent

    def test_a_stale_wait_is_rechecked_and_resume_goes_ahead(self):
        # The person pressed the knob after the last poll: M105 now answers.
        adapter, port = self._waiting_adapter()
        port.script["M105"] = M105_OK
        adapter._paused = True
        result = adapter.resume_print()
        assert result.success is True
        assert "M24" in port.sent
