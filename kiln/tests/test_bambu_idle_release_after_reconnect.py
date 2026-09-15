"""A connection paho restores on its own is watched for idleness again.

The idle reaper stops the first time it finds the connection down, and it was
started only on Kiln's own connect path.  paho reconnects after a drop by
itself, and that reconnect never passes Kiln's connect path, so a single
network blip left the printer's rationed LAN slot held for the rest of the
process.  The connect callback now restarts the reaper, which is idempotent.
"""

from __future__ import annotations

from typing import Any
from unittest import mock

import paho.mqtt.client as mqtt
import pytest

from kiln.printers.bambu import BambuAdapter


@pytest.fixture(autouse=True)
def _env(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KILN_BAMBU_TLS_PIN_FILE", str(tmp_path / "bambu_tls_pins.json"))
    monkeypatch.setenv("KILN_BAMBU_IDLE_DISCONNECT_S", "30")


def _adapter_and_client() -> tuple[BambuAdapter, mock.MagicMock]:
    adapter = BambuAdapter(host="192.0.2.10", access_code="12345678", serial="03900A000000001", timeout=2)
    adapter._confirm_window_s = 0.0
    client = mock.MagicMock()
    client.publish.return_value = mock.MagicMock(rc=mqtt.MQTT_ERR_SUCCESS)
    adapter._mqtt_client = client
    return adapter, client


def test_a_restored_connection_is_watched_again() -> None:
    adapter, client = _adapter_and_client()
    try:
        adapter._on_connect(client, None, {}, 0)
        reaper = adapter._idle_reaper
        assert reaper is not None and reaper.is_alive()
    finally:
        adapter._stop_idle_reaper()


def test_a_live_reaper_is_not_doubled() -> None:
    adapter, client = _adapter_and_client()
    try:
        adapter._on_connect(client, None, {}, 0)
        first = adapter._idle_reaper
        adapter._on_connect(client, None, {}, 0)
        assert adapter._idle_reaper is first
    finally:
        adapter._stop_idle_reaper()
