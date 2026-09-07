"""``send_gcode`` takes a list of whole lines, and says so instead of mangling.

Every adapter consumes the argument by iterating or joining it, so a bare
string is not a one-line script — it is one command per character.  Measured
on 2026-09-06 against the real adapters with only the transport mocked:

    send_gcode("M220 S50")   Klipper / Duet wire:  'M\\n2\\n2\\n0\\n \\nS\\n5\\n0'
                             USB serial:           8 separate writes
    send_gcode(["M220 S50"]) every wire:           'M220 S50'

Two callers had it — a calibration pipeline and a fleet speed tool — and
every layer above reported success.  The guard is in the base adapter so the
next caller to try it is refused at the door rather than silently corrupting
what the printer executes.
"""

from __future__ import annotations

from typing import Any
from unittest import mock

import pytest

from kiln.printers.base import PrinterError


def _adapters() -> list[tuple[str, Any, str]]:
    """(name, adapter, attribute to mock) for every adapter that sends G-code."""
    from kiln.printers.duet import DuetAdapter
    from kiln.printers.moonraker import MoonrakerAdapter
    from kiln.printers.octoprint import OctoPrintAdapter

    return [
        ("moonraker", MoonrakerAdapter(host="http://k.local"), "_post"),
        ("duet", DuetAdapter(host="http://duet.local"), "_request"),
        ("octoprint", OctoPrintAdapter(host="http://o.local", api_key="k"), "_post"),
    ]


@pytest.mark.parametrize("name,adapter,transport", _adapters(), ids=lambda v: v if isinstance(v, str) else "")
def test_a_bare_string_is_refused_not_split(name: str, adapter: Any, transport: str) -> None:
    with mock.patch.object(adapter, transport) as sent, pytest.raises(PrinterError, match="LIST of G-code lines"):
        adapter.send_gcode("M220 S50")
    sent.assert_not_called()  # nothing reached the printer


def test_the_refusal_shows_the_caller_the_fix() -> None:
    from kiln.printers.moonraker import MoonrakerAdapter

    adapter = MoonrakerAdapter(host="http://k.local")
    with mock.patch.object(adapter, "_post"), pytest.raises(PrinterError) as exc:
        adapter.send_gcode("G28")
    assert "['G28']" in str(exc.value)


def test_an_embedded_newline_is_refused_too() -> None:
    from kiln.printers.moonraker import MoonrakerAdapter

    adapter = MoonrakerAdapter(host="http://k.local")
    with mock.patch.object(adapter, "_post"), pytest.raises(PrinterError, match="ONE G-code line"):
        adapter.send_gcode(["G28\nG1 Z10"])


def test_a_proper_list_still_reaches_the_wire_whole() -> None:
    from kiln.printers.moonraker import MoonrakerAdapter

    adapter = MoonrakerAdapter(host="http://k.local")
    with mock.patch.object(adapter, "_post") as post:
        adapter.send_gcode(["M220 S50", "G28"])
    assert post.call_args.kwargs["params"]["script"] == "M220 S50\nG28"


def test_bambu_refuses_a_string_before_publishing() -> None:
    import paho.mqtt.client as mqtt

    from kiln.printers.bambu import BambuAdapter

    adapter = BambuAdapter(host="1.2.3.4", access_code="12345678", serial="01P00A000000001", timeout=2)
    adapter._mqtt_connected.set()
    adapter._connected = True
    adapter._mqtt_client = mock.MagicMock()
    adapter._mqtt_client.is_connected.return_value = True
    adapter._mqtt_client.publish.return_value = mock.MagicMock(rc=mqtt.MQTT_ERR_SUCCESS)

    with pytest.raises(PrinterError, match="LIST of G-code lines"):
        adapter.send_gcode("M220 S50")
    adapter._mqtt_client.publish.assert_not_called()
