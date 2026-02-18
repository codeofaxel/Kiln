"""Printer discovery -- find 3D printers on the local network.

Uses multiple discovery strategies:
1. mDNS/Bonjour (via zeroconf library if installed)
2. HTTP probe of common ports/paths
3. Manual IP scan of subnet

Follows the SonosCLI pattern: try multiple strategies, merge results,
return a unified list of discovered printers.
"""

from __future__ import annotations

import concurrent.futures
import logging
import platform
import socket
import time
from dataclasses import asdict, dataclass, field
from typing import Any

import requests  # already a kiln dependency

logger = logging.getLogger(__name__)


def _is_wsl() -> bool:
    """Return True if running inside WSL 2 (or 1)."""
    try:
        release = platform.release().lower()
        return "microsoft" in release or "wsl" in release
    except Exception as exc:
        logger.debug("WSL detection failed: %s", exc)
        return False


@dataclass
class DiscoveredPrinter:
    """A printer found on the network."""

    host: str
    port: int
    printer_type: str  # "octoprint", "moonraker", "bambu", "unknown"
    name: str = ""
    version: str = ""
    api_available: bool = False
    discovered_at: float = field(default_factory=time.time)
    discovery_method: str = ""  # "mdns", "http_probe", "manual"
    trusted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# Well-known ports and paths for each printer type
_PROBE_TARGETS = [
    # (port, path, expected_key, printer_type)
    (80, "/api/version", "text", "octoprint"),
    (5000, "/api/version", "text", "octoprint"),  # common OctoPrint port
    (7125, "/server/info", "klippy_state", "moonraker"),
    (80, "/server/info", "klippy_state", "moonraker"),
    (80, "/api/v1/status", "printer", "prusaconnect"),  # PrusaLink on default port
    (8080, "/api/v1/status", "printer", "prusaconnect"),  # PrusaLink alternate port
    (8883, None, None, "bambu"),  # Bambu MQTT port (no HTTP probe)
]

# mDNS service types to browse
_MDNS_SERVICES = [
    ("_octoprint._tcp.local.", "octoprint"),
    ("_moonraker._tcp.local.", "moonraker"),
    ("_http._tcp.local.", "unknown"),
]


def discover_printers(
    timeout: float = 5.0,
    subnet: str | None = None,
    methods: list[str] | None = None,
) -> list[DiscoveredPrinter]:
    """Run discovery using all available methods.

    Args:
        timeout: Maximum time in seconds for the entire discovery.
        subnet: Optional subnet to scan (e.g. "192.168.1"). If None,
            auto-detects from the default network interface.
        methods: List of methods to use. Default: ["mdns", "http_probe"].
            Options: "mdns", "http_probe".

    Returns:
        List of discovered printers, deduplicated by host+port.
    """
    if methods is None:
        methods = ["mdns", "http_probe"]

    if _is_wsl():
        if subnet is None:
            logger.warning(
                "Running on WSL 2. Auto-detected subnet will be the WSL "
                "virtual network, not your home network. For best results, "
                "provide the home network subnet explicitly, e.g. "
                "discover_printers(subnet='192.168.1')"
            )
        if "mdns" in methods:
            logger.info(
                "mDNS discovery is unreliable on WSL 2 due to NAT. "
                "Consider using explicit printer IPs or providing a subnet."
            )

    all_printers: list[DiscoveredPrinter] = []
    deadline = time.monotonic() + timeout

    for method in methods:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            logger.debug("Discovery timeout reached, skipping remaining methods")
            break

        try:
            if method == "mdns":
                all_printers.extend(_try_mdns(timeout=remaining))
            elif method == "http_probe":
                scan_subnet = subnet or _detect_subnet()
                if scan_subnet is None:
                    logger.warning("Could not detect subnet for HTTP probe; skipping")
                    continue
                all_printers.extend(_try_http_probe(scan_subnet, timeout=remaining))
            else:
                logger.warning("Unknown discovery method: %s", method)
        except Exception:
            logger.exception("Discovery method '%s' failed", method)

    deduped = _deduplicate(all_printers)
    _annotate_trust(deduped)
    return deduped


def probe_host(host: str, timeout: float = 3.0) -> list[DiscoveredPrinter]:
    """Probe a specific host for known printer services.

    Tries each known port/path combination and returns all matches.
    This is useful for verifying a manually-provided host.
    """
    results: list[DiscoveredPrinter] = []

    for port, path, expected_key, printer_type in _PROBE_TARGETS:
        # Skip Bambu MQTT-only entries (no HTTP probe possible)
        if path is None:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(timeout)
                result = sock.connect_ex((host, port))
                sock.close()
                if result == 0:
                    results.append(
                        DiscoveredPrinter(
                            host=host,
                            port=port,
                            printer_type=printer_type,
                            api_available=False,
                            discovery_method="manual",
                        )
                    )
            except OSError:
                pass
            continue

        url = f"http://{host}:{port}{path}"
        try:
            resp = requests.get(url, timeout=timeout)
            if resp.status_code == 200:
                data = resp.json()
                if expected_key in data:
                    name = data.get("text", data.get("hostname", ""))
                    version = ""
                    if printer_type == "octoprint":
                        version = data.get("server", "")
                    elif printer_type == "moonraker":
                        version = data.get("klippy_state", "")
                    elif printer_type == "prusaconnect":
                        printer_data = data.get("printer", {})
                        name = printer_data.get("state", "PrusaLink")
                        version = ""

                    results.append(
                        DiscoveredPrinter(
                            host=host,
                            port=port,
                            printer_type=printer_type,
                            name=str(name),
                            version=str(version),
                            api_available=True,
                            discovery_method="manual",
                        )
                    )
            elif resp.status_code == 401 and printer_type == "prusaconnect":
                # Prusa Link requires auth — 401 still means it's there
                results.append(
                    DiscoveredPrinter(
                        host=host,
                        port=port,
                        printer_type=printer_type,
                        name="PrusaLink",
                        api_available=True,
                        discovery_method="manual",
                    )
                )
        except (requests.ConnectionError, requests.Timeout, requests.JSONDecodeError, OSError):
            pass

    return results


def _try_mdns(timeout: float) -> list[DiscoveredPrinter]:
    """Discover printers via mDNS/Bonjour (requires zeroconf)."""
    try:
        from zeroconf import ServiceBrowser, Zeroconf  # type: ignore[import-untyped]
    except ImportError:
        logger.debug("zeroconf library not installed; skipping mDNS discovery. Install it with: pip install zeroconf")
        return []

    results: list[DiscoveredPrinter] = []

    class _Listener:
        """Collect discovered services."""

        def __init__(self, printer_type: str) -> None:
            self.printer_type = printer_type

        def add_service(self, zc: Zeroconf, type_: str, name: str) -> None:
            info = zc.get_service_info(type_, name)
            if info is None:
                return
            addresses = info.parsed_addresses()
            if not addresses:
                return
            host = addresses[0]
            port = info.port or 80
            server_name = info.server or name
            results.append(
                DiscoveredPrinter(
                    host=host,
                    port=port,
                    printer_type=self.printer_type,
                    name=server_name,
                    api_available=True,
                    discovery_method="mdns",
                )
            )

        def remove_service(self, zc: Zeroconf, type_: str, name: str) -> None:
            pass

        def update_service(self, zc: Zeroconf, type_: str, name: str) -> None:
            pass

    zc = Zeroconf()
    browsers = []
    try:
        for service_type, printer_type in _MDNS_SERVICES:
            listener = _Listener(printer_type)
            browser = ServiceBrowser(zc, service_type, listener)
            browsers.append(browser)

        # Wait for responses up to the timeout
        time.sleep(min(timeout, 5.0))
    finally:
        zc.close()

    return results


def _try_http_probe(subnet: str, timeout: float) -> list[DiscoveredPrinter]:
    """Probe common printer ports on the local subnet.

    Scans hosts 1-254 in the given subnet using a thread pool for
    concurrent probing.
    """
    results: list[DiscoveredPrinter] = []
    per_host_timeout = min(timeout / 10, 2.0)  # keep individual probes short

    def _probe_single(host_num: int) -> list[DiscoveredPrinter]:
        host = f"{subnet}.{host_num}"
        found: list[DiscoveredPrinter] = []

        for port, path, expected_key, printer_type in _PROBE_TARGETS:
            if path is None:
                continue  # skip non-HTTP probes in subnet scan

            url = f"http://{host}:{port}{path}"
            try:
                resp = requests.get(url, timeout=per_host_timeout)
                if resp.status_code == 200:
                    data = resp.json()
                    if expected_key in data:
                        name = data.get("text", data.get("hostname", ""))
                        version = ""
                        if printer_type == "octoprint":
                            version = data.get("server", "")
                        elif printer_type == "moonraker":
                            version = data.get("klippy_state", "")
                        elif printer_type == "prusaconnect":
                            printer_data = data.get("printer", {})
                            name = printer_data.get("state", "PrusaLink")
                            version = ""

                        found.append(
                            DiscoveredPrinter(
                                host=host,
                                port=port,
                                printer_type=printer_type,
                                name=str(name),
                                version=str(version),
                                api_available=True,
                                discovery_method="http_probe",
                            )
                        )
                elif resp.status_code == 401 and printer_type == "prusaconnect":
                    # Prusa Link requires auth — 401 still means it's there
                    found.append(
                        DiscoveredPrinter(
                            host=host,
                            port=port,
                            printer_type=printer_type,
                            name="PrusaLink",
                            api_available=True,
                            discovery_method="http_probe",
                        )
                    )
            except (
                requests.ConnectionError,
                requests.Timeout,
                requests.JSONDecodeError,
                OSError,
            ):
                pass

        return found

    with concurrent.futures.ThreadPoolExecutor(max_workers=50) as executor:
        futures = {executor.submit(_probe_single, i): i for i in range(1, 255)}
        done, _ = concurrent.futures.wait(futures, timeout=timeout, return_when=concurrent.futures.ALL_COMPLETED)
        for future in done:
            try:
                found = future.result(timeout=0)
                results.extend(found)
            except Exception as exc:
                logger.debug("Failed to get result from discovery probe future: %s", exc)

    return results


def _detect_subnet() -> str | None:
    """Auto-detect the local subnet from the default interface.

    Returns the first three octets of the host's IP (e.g. "192.168.1"),
    or None if detection fails.

    On WSL 2, the default interface returns the virtual network
    (172.x.x.x). This function attempts to detect the Windows host
    gateway and use its subnet instead, since printers are typically
    on the same network as the Windows host.
    """
    try:
        hostname = socket.gethostname()
        ip = socket.gethostbyname(hostname)

        # gethostbyname may return 127.x.x.x on some systems; try a
        # UDP connect trick to find the real LAN address.
        if ip.startswith("127."):
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                # Does not actually send data; just determines the
                # outbound interface.
                s.connect(("10.255.255.255", 1))
                ip = s.getsockname()[0]

        # On WSL 2, the detected IP is on the virtual NAT network
        # (typically 172.x.x.x). Try to find the Windows host gateway
        # which is usually on the home network.
        if _is_wsl() and ip.startswith("172."):
            wsl_subnet = _detect_wsl_host_subnet()
            if wsl_subnet:
                logger.info(
                    "WSL 2 detected. Using Windows host subnet %s instead of virtual network %s",
                    wsl_subnet,
                    ip,
                )
                return wsl_subnet

        parts = ip.split(".")
        if len(parts) == 4:
            return ".".join(parts[:3])
    except OSError:
        logger.debug("Failed to detect local subnet", exc_info=True)

    return None


def _detect_wsl_host_subnet() -> str | None:
    """Try to detect the Windows host's home network subnet from WSL 2.

    Reads /etc/resolv.conf which WSL 2 populates with the Windows host
    IP as the nameserver. This IP is on the host's network and can be
    used to derive the subnet for printer discovery.
    """
    try:
        with open("/etc/resolv.conf") as f:
            for line in f:
                line = line.strip()
                if line.startswith("nameserver"):
                    parts = line.split()
                    if len(parts) >= 2:
                        ns_ip = parts[1]
                        octets = ns_ip.split(".")
                        if len(octets) == 4 and not ns_ip.startswith("172."):
                            # The nameserver in WSL 2 is typically the
                            # Windows host IP on the virtual network.
                            # If it's 172.x.x.x it's the vEthernet
                            # adapter, not the home network.
                            return ".".join(octets[:3])
    except OSError:
        pass
    return None


def _annotate_trust(printers: list[DiscoveredPrinter]) -> None:
    """Mark each printer as trusted or untrusted based on the config whitelist."""
    try:
        from kiln.cli.config import get_trusted_printers

        trusted = get_trusted_printers()
    except Exception as exc:
        # Config may not be available in all environments.
        logger.debug("Failed to load trusted printers config: %s", exc)
        return

    for p in printers:
        if p.host in trusted:
            p.trusted = True


def _deduplicate(
    printers: list[DiscoveredPrinter],
) -> list[DiscoveredPrinter]:
    """Remove duplicate printers, preferring the entry with more detail.

    Deduplicates by (host, port). When two entries share the same key,
    the one with ``api_available=True`` wins; otherwise the first one
    encountered is kept.
    """
    seen: dict[tuple[str, int], DiscoveredPrinter] = {}
    for p in printers:
        key = (p.host, p.port)
        existing = seen.get(key)
        if existing is None or p.api_available and not existing.api_available:
            seen[key] = p
        # else keep existing

    return list(seen.values())
