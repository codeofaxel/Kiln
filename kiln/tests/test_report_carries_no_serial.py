"""A bug report leaves this computer with no printer serial in it.

The labelled rule lives in the shared redaction module, so the report door
here and Kiln's servers apply the same one.  This computer's own serials
also leave a log tail as their one-way code, wherever they appear.
"""

from __future__ import annotations

import pytest

from kiln import outbound_guard as guard
from kiln.redaction import redact_for_report, redact_machine_text

SERIAL = "22E8BJ652001675"
ADDRESS = "192.168.1.40"
CONFIG_LINE = (
    "kiln.server INFO Kiln printer config source: ~/.kiln/config.yaml "
    f"(host={ADDRESS}, type=bambu, api_key=abc123secret, serial={SERIAL})"
)


@pytest.mark.parametrize("text", [
    CONFIG_LINE,
    f'{{"serial": "{SERIAL}", "state": "idle"}}',
    f"serial_number: {SERIAL}",
    f"printer_serial={SERIAL}",
    f"dev_id={SERIAL}",
    f"sn={SERIAL}",
    f"Subscribed to device/{SERIAL}/report",
])
def test_a_labelled_serial_never_leaves(text: str) -> None:
    for redact in (redact_for_report, redact_machine_text):
        assert SERIAL not in redact(text), redact.__name__


@pytest.mark.parametrize("text", ["serial=(none)", "serial port /dev/ttyUSB0 open", "sn=12", "a design session"])
def test_words_that_are_not_a_serial_stay(text: str) -> None:
    assert redact_for_report(text) == text


def _own(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(guard, "_own_printer_values", lambda: [
        ("the printer's serial number", SERIAL),
        ("the printer's network address", ADDRESS),
    ])


def test_this_computers_own_serial_leaves_a_log_tail_as_its_code(monkeypatch: pytest.MonkeyPatch) -> None:
    _own(monkeypatch)
    out = guard.redact_log_text(f"MQTT connected to {SERIAL} at {ADDRESS}; reconnecting to {SERIAL}")
    assert SERIAL not in out
    assert ADDRESS not in out
    assert out.count(guard.machine_code(SERIAL)) == 2


def test_the_report_door_sends_the_tail_without_the_serial(monkeypatch: pytest.MonkeyPatch) -> None:
    from kiln import server

    _own(monkeypatch)
    monkeypatch.setattr("kiln.log_config.read_log_tail", lambda *a, **k: CONFIG_LINE)
    sent = server._with_local_log_tail({"context": {"os": "Windows 11"}})
    tail = sent["context"]["log_tail"]
    assert SERIAL not in tail
    assert ADDRESS not in tail
    assert "abc123secret" not in tail
