"""``kiln doctor --deep`` probes the host and ports a printer really uses.

The saved ``host`` of every HTTP backend is a URL (``http://octopi.local:5000``),
so the probe has to take the hostname out of it before pinging or opening a
socket, and has to try the port the URL names before any default.
"""

from __future__ import annotations

import socket
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from kiln.cli.main import _deep_network_diagnostics


def _run(host: str, printer_type: str) -> tuple[list[str], list[tuple[str, int]]]:
    """Run the probe with ping succeeding and every port refused.

    Returns the ping argv and the (host, port) pairs the probe opened.
    """
    pinged: list[str] = []
    probed: list[tuple[str, int]] = []

    def fake_run(argv, **_kwargs):
        pinged.extend(argv)
        return MagicMock(returncode=0)

    def fake_connect(address, timeout=None):
        probed.append((address[0], address[1]))
        raise ConnectionRefusedError

    with (
        patch.object(subprocess, "run", side_effect=fake_run),
        patch.object(socket, "create_connection", side_effect=fake_connect),
    ):
        _deep_network_diagnostics(host, {"type": printer_type, "host": host})
    return pinged, probed


class TestHostIsTakenOutOfTheUrl:
    def test_octoprint_url_is_pinged_and_probed_by_hostname(self):
        pinged, probed = _run("http://192.168.1.50:5000", "octoprint")
        assert "192.168.1.50" in pinged
        assert all(h == "192.168.1.50" for h, _ in probed), probed

    def test_the_port_the_url_names_is_probed_first(self):
        _, probed = _run("http://192.168.1.50:5000", "octoprint")
        assert probed[0] == ("192.168.1.50", 5000)

    def test_https_url_without_a_port_probes_443_first(self):
        _, probed = _run("https://prusa.local", "prusalink")
        assert probed[0] == ("prusa.local", 443)


class TestDefaultPortsComeFromTheMakers:
    def test_bare_octoprint_host_tries_80_then_5000(self):
        _, probed = _run("http://octopi.local", "octoprint")
        ports = [p for _, p in probed]
        assert ports[:2] == [80, 5000], ports

    def test_elegoo_probes_the_tcp_service_port_not_udp_discovery(self):
        _, probed = _run("192.168.1.60", "elegoo")
        ports = [p for _, p in probed]
        assert 3030 in ports
        assert 3000 not in ports, "3000 is UDP discovery; a TCP probe of it tests nothing"

    @pytest.mark.parametrize("printer_type", ["moonraker", "creality", "duet"])
    def test_other_http_backends_keep_their_hostname(self, printer_type):
        _, probed = _run("http://10.0.0.7", printer_type)
        assert probed and all(h == "10.0.0.7" for h, _ in probed)
