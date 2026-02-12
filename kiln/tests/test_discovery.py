"""Tests for kiln.discovery -- network printer discovery.

Covers:
- DiscoveredPrinter dataclass and to_dict
- probe_host with mocked HTTP responses
- _detect_subnet with mocked socket calls
- discover_printers with all methods mocked
- _try_mdns when zeroconf is not installed
- _try_http_probe with mocked concurrent futures
- Deduplication (same host found by multiple methods)
- Timeout handling
- probe_host with connection refused
"""

from __future__ import annotations

import concurrent.futures
import socket
import time
from unittest.mock import MagicMock, patch

import pytest
import requests
import responses

from kiln.discovery import (
    DiscoveredPrinter,
    _deduplicate,
    _detect_subnet,
    _try_http_probe,
    _try_mdns,
    discover_printers,
    probe_host,
)


# ---------------------------------------------------------------------------
# DiscoveredPrinter dataclass
# ---------------------------------------------------------------------------


class TestDiscoveredPrinter:
    """Tests for the DiscoveredPrinter dataclass."""

    def test_basic_creation(self):
        p = DiscoveredPrinter(
            host="192.168.1.100",
            port=80,
            printer_type="octoprint",
        )
        assert p.host == "192.168.1.100"
        assert p.port == 80
        assert p.printer_type == "octoprint"
        assert p.name == ""
        assert p.version == ""
        assert p.api_available is False
        assert p.discovery_method == ""

    def test_full_creation(self):
        p = DiscoveredPrinter(
            host="192.168.1.50",
            port=7125,
            printer_type="moonraker",
            name="Voron-24",
            version="ready",
            api_available=True,
            discovered_at=1700000000.0,
            discovery_method="mdns",
        )
        assert p.name == "Voron-24"
        assert p.version == "ready"
        assert p.api_available is True
        assert p.discovered_at == 1700000000.0
        assert p.discovery_method == "mdns"

    def test_to_dict(self):
        p = DiscoveredPrinter(
            host="10.0.0.5",
            port=5000,
            printer_type="octoprint",
            name="Ender3",
            version="1.9.3",
            api_available=True,
            discovered_at=1700000000.0,
            discovery_method="http_probe",
        )
        d = p.to_dict()
        assert isinstance(d, dict)
        assert d["host"] == "10.0.0.5"
        assert d["port"] == 5000
        assert d["printer_type"] == "octoprint"
        assert d["name"] == "Ender3"
        assert d["version"] == "1.9.3"
        assert d["api_available"] is True
        assert d["discovered_at"] == 1700000000.0
        assert d["discovery_method"] == "http_probe"

    def test_to_dict_returns_new_dict(self):
        p = DiscoveredPrinter(host="1.2.3.4", port=80, printer_type="unknown")
        d1 = p.to_dict()
        d2 = p.to_dict()
        assert d1 == d2
        assert d1 is not d2

    def test_discovered_at_default(self):
        before = time.time()
        p = DiscoveredPrinter(host="1.2.3.4", port=80, printer_type="unknown")
        after = time.time()
        assert before <= p.discovered_at <= after


# ---------------------------------------------------------------------------
# probe_host
# ---------------------------------------------------------------------------


class TestProbeHost:
    """Tests for probe_host with mocked HTTP responses."""

    @responses.activate
    def test_octoprint_found_port_80(self):
        responses.add(
            responses.GET,
            "http://192.168.1.100:80/api/version",
            json={"text": "OctoPrint", "server": "1.9.3"},
            status=200,
        )
        # Other probes will fail with ConnectionError (no mock registered)
        results = probe_host("192.168.1.100", timeout=1.0)
        octoprint_results = [r for r in results if r.printer_type == "octoprint"]
        assert len(octoprint_results) >= 1
        p = octoprint_results[0]
        assert p.host == "192.168.1.100"
        assert p.port == 80
        assert p.printer_type == "octoprint"
        assert p.name == "OctoPrint"
        assert p.version == "1.9.3"
        assert p.api_available is True
        assert p.discovery_method == "manual"

    @responses.activate
    def test_octoprint_found_port_5000(self):
        responses.add(
            responses.GET,
            "http://192.168.1.100:5000/api/version",
            json={"text": "OctoPrint", "server": "1.10.0"},
            status=200,
        )
        results = probe_host("192.168.1.100", timeout=1.0)
        match = [r for r in results if r.port == 5000]
        assert len(match) == 1
        assert match[0].printer_type == "octoprint"
        assert match[0].version == "1.10.0"

    @responses.activate
    def test_moonraker_found(self):
        responses.add(
            responses.GET,
            "http://10.0.0.50:7125/server/info",
            json={"klippy_state": "ready", "hostname": "voron"},
            status=200,
        )
        results = probe_host("10.0.0.50", timeout=1.0)
        moonraker = [r for r in results if r.printer_type == "moonraker"]
        assert len(moonraker) >= 1
        p = moonraker[0]
        assert p.host == "10.0.0.50"
        assert p.port == 7125
        assert p.printer_type == "moonraker"
        assert p.version == "ready"
        assert p.api_available is True

    @responses.activate
    def test_nothing_found(self):
        """All probes fail with ConnectionError -- no printers found."""
        # responses library raises ConnectionError for unregistered URLs
        results = probe_host("192.168.1.200", timeout=0.5)
        # May find bambu via socket if something is listening, but with
        # mocked socket below it won't.  Filter to HTTP-probed results.
        http_results = [r for r in results if r.discovery_method == "manual" and r.api_available]
        assert http_results == []

    @responses.activate
    def test_connection_refused_returns_empty(self):
        """Connection refused on all ports returns empty list."""
        # No responses registered means ConnectionError for all HTTP probes.
        # Patch socket for the bambu MQTT port check.
        with patch("kiln.discovery.socket.socket") as mock_sock:
            mock_instance = MagicMock()
            mock_instance.connect_ex.return_value = 111  # ECONNREFUSED
            mock_sock.return_value = mock_instance
            results = probe_host("192.168.1.99", timeout=0.5)
        assert results == []

    @responses.activate
    def test_non_200_response_ignored(self):
        responses.add(
            responses.GET,
            "http://192.168.1.100:80/api/version",
            json={"error": "unauthorized"},
            status=403,
        )
        results = probe_host("192.168.1.100", timeout=1.0)
        port80_octo = [r for r in results if r.port == 80 and r.printer_type == "octoprint"]
        assert port80_octo == []

    @responses.activate
    def test_missing_expected_key_ignored(self):
        """Response is 200 but does not contain the expected key."""
        responses.add(
            responses.GET,
            "http://192.168.1.100:80/api/version",
            json={"something": "else"},
            status=200,
        )
        results = probe_host("192.168.1.100", timeout=1.0)
        port80_octo = [r for r in results if r.port == 80 and r.printer_type == "octoprint"]
        assert port80_octo == []

    @responses.activate
    def test_prusaconnect_found_port_80(self):
        """PrusaLink printer detected on port 80 via /api/v1/status."""
        responses.add(
            responses.GET,
            "http://192.168.1.77:80/api/v1/status",
            json={
                "printer": {"state": "IDLE", "temp_nozzle": 25.0, "temp_bed": 22.0},
                "job": {},
            },
            status=200,
        )
        results = probe_host("192.168.1.77", timeout=1.0)
        prusa = [r for r in results if r.printer_type == "prusaconnect"]
        assert len(prusa) >= 1
        p = prusa[0]
        assert p.host == "192.168.1.77"
        assert p.port == 80
        assert p.printer_type == "prusaconnect"
        assert p.api_available is True
        assert p.discovery_method == "manual"

    @responses.activate
    def test_prusaconnect_found_port_8080(self):
        """PrusaLink printer detected on alternate port 8080."""
        responses.add(
            responses.GET,
            "http://192.168.1.77:8080/api/v1/status",
            json={
                "printer": {"state": "PRINTING", "temp_nozzle": 215.0},
                "job": {"id": 1, "progress": 42.0},
            },
            status=200,
        )
        results = probe_host("192.168.1.77", timeout=1.0)
        prusa = [r for r in results if r.printer_type == "prusaconnect" and r.port == 8080]
        assert len(prusa) == 1
        assert prusa[0].api_available is True

    @responses.activate
    def test_bambu_port_open(self):
        """Bambu detected when MQTT port is open."""
        with patch("kiln.discovery.socket.socket") as mock_sock:
            mock_instance = MagicMock()
            mock_instance.connect_ex.return_value = 0  # success
            mock_sock.return_value = mock_instance
            results = probe_host("192.168.1.55", timeout=1.0)
        bambu = [r for r in results if r.printer_type == "bambu"]
        assert len(bambu) == 1
        assert bambu[0].port == 8883
        assert bambu[0].api_available is False


# ---------------------------------------------------------------------------
# _detect_subnet
# ---------------------------------------------------------------------------


class TestDetectSubnet:
    """Tests for _detect_subnet with mocked socket calls."""

    def test_normal_ip(self):
        with patch("kiln.discovery.socket.gethostname", return_value="myhost"), \
             patch("kiln.discovery.socket.gethostbyname", return_value="192.168.1.42"):
            result = _detect_subnet()
        assert result == "192.168.1"

    def test_ten_network(self):
        with patch("kiln.discovery.socket.gethostname", return_value="workstation"), \
             patch("kiln.discovery.socket.gethostbyname", return_value="10.0.0.15"):
            result = _detect_subnet()
        assert result == "10.0.0"

    def test_loopback_falls_back_to_udp_trick(self):
        """When gethostbyname returns 127.x, use the UDP connect trick."""
        mock_sock = MagicMock()
        mock_sock.getsockname.return_value = ("172.16.0.100", 0)

        with patch("kiln.discovery.socket.gethostname", return_value="localhost"), \
             patch("kiln.discovery.socket.gethostbyname", return_value="127.0.0.1"), \
             patch("kiln.discovery.socket.socket") as sock_cls:
            sock_cls.return_value.__enter__ = MagicMock(return_value=mock_sock)
            sock_cls.return_value.__exit__ = MagicMock(return_value=False)
            result = _detect_subnet()
        assert result == "172.16.0"

    def test_oserror_returns_none(self):
        with patch("kiln.discovery.socket.gethostname", side_effect=OSError("fail")):
            result = _detect_subnet()
        assert result is None


# ---------------------------------------------------------------------------
# _try_mdns
# ---------------------------------------------------------------------------


class TestTryMdns:
    """Tests for _try_mdns."""

    def test_zeroconf_not_installed_returns_empty(self):
        """When zeroconf is not importable, return empty list without error."""
        with patch.dict("sys.modules", {"zeroconf": None}):
            result = _try_mdns(timeout=1.0)
        assert result == []

    def test_zeroconf_import_error_returns_empty(self):
        """When importing zeroconf raises ImportError, return empty."""
        import sys
        original = sys.modules.get("zeroconf")
        sys.modules["zeroconf"] = None  # type: ignore[assignment]
        try:
            result = _try_mdns(timeout=0.1)
            assert result == []
        finally:
            if original is not None:
                sys.modules["zeroconf"] = original
            else:
                sys.modules.pop("zeroconf", None)

    def test_zeroconf_available_discovers_printers(self):
        """When zeroconf is available, printers are discovered via mDNS."""
        # Create mock zeroconf module and objects
        mock_service_info = MagicMock()
        mock_service_info.parsed_addresses.return_value = ["192.168.1.50"]
        mock_service_info.port = 80
        mock_service_info.server = "octoprinter.local."

        mock_zc_instance = MagicMock()
        mock_zc_instance.get_service_info.return_value = mock_service_info

        mock_zeroconf_cls = MagicMock(return_value=mock_zc_instance)

        # ServiceBrowser should call add_service on the listener
        def fake_service_browser(zc, service_type, listener):
            # Simulate discovering a service
            listener.add_service(zc, service_type, "OctoPrint._octoprint._tcp.local.")
            return MagicMock()

        mock_module = MagicMock()
        mock_module.Zeroconf = mock_zeroconf_cls
        mock_module.ServiceBrowser = fake_service_browser

        with patch.dict("sys.modules", {"zeroconf": mock_module}):
            # Need to also patch the actual import inside the function
            with patch("kiln.discovery.time.sleep"):
                result = _try_mdns(timeout=1.0)

        assert len(result) >= 1
        # Find the one from the _octoprint._tcp service
        octo = [r for r in result if r.printer_type == "octoprint"]
        assert len(octo) >= 1
        assert octo[0].host == "192.168.1.50"
        assert octo[0].discovery_method == "mdns"


# ---------------------------------------------------------------------------
# _try_http_probe
# ---------------------------------------------------------------------------


class TestTryHttpProbe:
    """Tests for _try_http_probe with mocked HTTP requests."""

    @responses.activate
    def test_finds_octoprint_on_subnet(self):
        """Discovers an OctoPrint instance on the subnet."""
        responses.add(
            responses.GET,
            "http://192.168.1.42:80/api/version",
            json={"text": "OctoPrint", "server": "1.9.3"},
            status=200,
        )
        results = _try_http_probe("192.168.1", timeout=10.0)
        octo = [r for r in results if r.printer_type == "octoprint"]
        assert len(octo) >= 1
        p = octo[0]
        assert p.host == "192.168.1.42"
        assert p.port == 80
        assert p.api_available is True
        assert p.discovery_method == "http_probe"

    @responses.activate
    def test_finds_moonraker_on_subnet(self):
        """Discovers a Moonraker instance on the subnet."""
        responses.add(
            responses.GET,
            "http://10.0.0.10:7125/server/info",
            json={"klippy_state": "ready", "hostname": "klipper-box"},
            status=200,
        )
        results = _try_http_probe("10.0.0", timeout=10.0)
        moon = [r for r in results if r.printer_type == "moonraker"]
        assert len(moon) >= 1
        assert moon[0].host == "10.0.0.10"
        assert moon[0].port == 7125

    @responses.activate
    def test_no_printers_found(self):
        """No printers on the subnet -- all probes fail."""
        results = _try_http_probe("10.99.99", timeout=5.0)
        assert results == []

    @responses.activate
    def test_finds_prusaconnect_on_subnet(self):
        """Discovers a PrusaLink printer on the subnet."""
        responses.add(
            responses.GET,
            "http://192.168.1.77:80/api/v1/status",
            json={
                "printer": {"state": "IDLE", "temp_nozzle": 25.0},
                "job": {},
            },
            status=200,
        )
        results = _try_http_probe("192.168.1", timeout=10.0)
        prusa = [r for r in results if r.printer_type == "prusaconnect"]
        assert len(prusa) >= 1
        p = prusa[0]
        assert p.host == "192.168.1.77"
        assert p.port == 80
        assert p.api_available is True
        assert p.discovery_method == "http_probe"

    @responses.activate
    def test_multiple_printers_found(self):
        """Multiple printers on the same subnet."""
        responses.add(
            responses.GET,
            "http://192.168.1.10:80/api/version",
            json={"text": "OctoPrint", "server": "1.9.0"},
            status=200,
        )
        responses.add(
            responses.GET,
            "http://192.168.1.20:7125/server/info",
            json={"klippy_state": "ready"},
            status=200,
        )
        results = _try_http_probe("192.168.1", timeout=10.0)
        types = {r.printer_type for r in results}
        assert "octoprint" in types
        assert "moonraker" in types


# ---------------------------------------------------------------------------
# discover_printers (integration with mocked methods)
# ---------------------------------------------------------------------------


class TestDiscoverPrinters:
    """Tests for discover_printers with mocked discovery methods."""

    def test_default_methods(self):
        mdns_printer = DiscoveredPrinter(
            host="192.168.1.10", port=80, printer_type="octoprint",
            name="OctoPrint-mdns", discovery_method="mdns", api_available=True,
        )
        http_printer = DiscoveredPrinter(
            host="192.168.1.20", port=7125, printer_type="moonraker",
            name="Moonraker-http", discovery_method="http_probe", api_available=True,
        )

        with patch("kiln.discovery._try_mdns", return_value=[mdns_printer]) as mock_mdns, \
             patch("kiln.discovery._try_http_probe", return_value=[http_printer]) as mock_http, \
             patch("kiln.discovery._detect_subnet", return_value="192.168.1"):
            results = discover_printers(timeout=5.0)

        mock_mdns.assert_called_once()
        mock_http.assert_called_once()
        assert len(results) == 2
        hosts = {r.host for r in results}
        assert "192.168.1.10" in hosts
        assert "192.168.1.20" in hosts

    def test_mdns_only(self):
        mdns_printer = DiscoveredPrinter(
            host="192.168.1.10", port=80, printer_type="octoprint",
            discovery_method="mdns",
        )
        with patch("kiln.discovery._try_mdns", return_value=[mdns_printer]):
            results = discover_printers(timeout=5.0, methods=["mdns"])
        assert len(results) == 1
        assert results[0].host == "192.168.1.10"

    def test_http_probe_only(self):
        http_printer = DiscoveredPrinter(
            host="10.0.0.5", port=5000, printer_type="octoprint",
            discovery_method="http_probe",
        )
        with patch("kiln.discovery._try_http_probe", return_value=[http_printer]), \
             patch("kiln.discovery._detect_subnet", return_value="10.0.0"):
            results = discover_printers(timeout=5.0, methods=["http_probe"])
        assert len(results) == 1
        assert results[0].host == "10.0.0.5"

    def test_custom_subnet(self):
        with patch("kiln.discovery._try_http_probe", return_value=[]) as mock_http:
            discover_printers(
                timeout=5.0, subnet="172.16.0", methods=["http_probe"]
            )
        # Should have been called with the provided subnet
        args, kwargs = mock_http.call_args
        assert args[0] == "172.16.0"

    def test_no_subnet_detected_skips_http(self):
        with patch("kiln.discovery._detect_subnet", return_value=None), \
             patch("kiln.discovery._try_http_probe") as mock_http:
            results = discover_printers(
                timeout=5.0, methods=["http_probe"]
            )
        mock_http.assert_not_called()
        assert results == []

    def test_method_failure_does_not_crash(self):
        """If one method raises, the others still run."""
        http_printer = DiscoveredPrinter(
            host="192.168.1.20", port=80, printer_type="octoprint",
            discovery_method="http_probe",
        )
        with patch("kiln.discovery._try_mdns", side_effect=RuntimeError("boom")), \
             patch("kiln.discovery._try_http_probe", return_value=[http_printer]), \
             patch("kiln.discovery._detect_subnet", return_value="192.168.1"):
            results = discover_printers(timeout=5.0)
        assert len(results) == 1
        assert results[0].host == "192.168.1.20"

    def test_unknown_method_logged_and_skipped(self):
        with patch("kiln.discovery._try_mdns", return_value=[]):
            results = discover_printers(
                timeout=5.0, methods=["mdns", "carrier_pigeon"]
            )
        assert results == []

    def test_empty_methods(self):
        results = discover_printers(timeout=1.0, methods=[])
        assert results == []


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------


class TestDeduplication:
    """Tests for deduplication logic."""

    def test_same_host_port_deduplicated(self):
        p1 = DiscoveredPrinter(
            host="192.168.1.10", port=80, printer_type="octoprint",
            discovery_method="mdns", api_available=True,
        )
        p2 = DiscoveredPrinter(
            host="192.168.1.10", port=80, printer_type="octoprint",
            discovery_method="http_probe", api_available=True,
        )
        result = _deduplicate([p1, p2])
        assert len(result) == 1

    def test_different_ports_not_deduplicated(self):
        p1 = DiscoveredPrinter(
            host="192.168.1.10", port=80, printer_type="octoprint",
            api_available=True,
        )
        p2 = DiscoveredPrinter(
            host="192.168.1.10", port=5000, printer_type="octoprint",
            api_available=True,
        )
        result = _deduplicate([p1, p2])
        assert len(result) == 2

    def test_different_hosts_not_deduplicated(self):
        p1 = DiscoveredPrinter(
            host="192.168.1.10", port=80, printer_type="octoprint",
        )
        p2 = DiscoveredPrinter(
            host="192.168.1.20", port=80, printer_type="moonraker",
        )
        result = _deduplicate([p1, p2])
        assert len(result) == 2

    def test_api_available_preferred(self):
        """When deduplicating, prefer the entry with api_available=True."""
        p_no_api = DiscoveredPrinter(
            host="192.168.1.10", port=80, printer_type="unknown",
            discovery_method="mdns", api_available=False,
        )
        p_with_api = DiscoveredPrinter(
            host="192.168.1.10", port=80, printer_type="octoprint",
            discovery_method="http_probe", api_available=True,
        )
        result = _deduplicate([p_no_api, p_with_api])
        assert len(result) == 1
        assert result[0].api_available is True
        assert result[0].printer_type == "octoprint"

    def test_first_api_available_kept(self):
        """When both have api_available, the first one wins."""
        p1 = DiscoveredPrinter(
            host="192.168.1.10", port=80, printer_type="octoprint",
            discovery_method="mdns", api_available=True, name="first",
        )
        p2 = DiscoveredPrinter(
            host="192.168.1.10", port=80, printer_type="octoprint",
            discovery_method="http_probe", api_available=True, name="second",
        )
        result = _deduplicate([p1, p2])
        assert len(result) == 1
        assert result[0].name == "first"

    def test_discover_printers_deduplicates_across_methods(self):
        """discover_printers merges and deduplicates results."""
        printer_mdns = DiscoveredPrinter(
            host="192.168.1.10", port=80, printer_type="unknown",
            discovery_method="mdns", api_available=False,
        )
        printer_http = DiscoveredPrinter(
            host="192.168.1.10", port=80, printer_type="octoprint",
            discovery_method="http_probe", api_available=True, name="OctoPrint",
        )
        with patch("kiln.discovery._try_mdns", return_value=[printer_mdns]), \
             patch("kiln.discovery._try_http_probe", return_value=[printer_http]), \
             patch("kiln.discovery._detect_subnet", return_value="192.168.1"):
            results = discover_printers(timeout=5.0)
        assert len(results) == 1
        assert results[0].api_available is True
        assert results[0].name == "OctoPrint"

    def test_empty_list(self):
        result = _deduplicate([])
        assert result == []


# ---------------------------------------------------------------------------
# Timeout handling
# ---------------------------------------------------------------------------


class TestTimeoutHandling:
    """Tests for timeout behavior."""

    def test_short_timeout_still_returns(self):
        """Even with a very short timeout, the function returns without error."""
        with patch("kiln.discovery._try_mdns", return_value=[]), \
             patch("kiln.discovery._try_http_probe", return_value=[]), \
             patch("kiln.discovery._detect_subnet", return_value="192.168.1"):
            results = discover_printers(timeout=0.001)
        assert isinstance(results, list)

    def test_timeout_skips_later_methods(self):
        """If deadline passes during first method, later methods are skipped."""
        def slow_mdns(timeout: float) -> list:
            # Simulate slow mDNS that uses up the entire timeout
            return []

        with patch("kiln.discovery._try_mdns", side_effect=slow_mdns), \
             patch("kiln.discovery._try_http_probe") as mock_http, \
             patch("kiln.discovery._detect_subnet", return_value="192.168.1"), \
             patch("kiln.discovery.time.monotonic") as mock_monotonic:
            # First call: start time. Second call: within deadline.
            # Third call (before http_probe): past deadline.
            mock_monotonic.side_effect = [0.0, 0.0, 100.0]
            results = discover_printers(timeout=5.0)

        mock_http.assert_not_called()
        assert results == []

    def test_probe_host_timeout_parameter_respected(self):
        """probe_host passes the timeout to requests."""
        with patch("kiln.discovery.requests.get") as mock_get, \
             patch("kiln.discovery.socket.socket") as mock_sock:
            mock_get.side_effect = requests.ConnectionError("timeout")
            mock_instance = MagicMock()
            mock_instance.connect_ex.return_value = 111
            mock_sock.return_value = mock_instance
            probe_host("192.168.1.1", timeout=0.5)

        # Verify requests.get was called with timeout=0.5
        for call in mock_get.call_args_list:
            assert call.kwargs.get("timeout") == 0.5
