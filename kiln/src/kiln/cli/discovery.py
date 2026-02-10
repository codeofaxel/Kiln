"""Network discovery for 3D printers via mDNS (Zeroconf).

Scans the local network for OctoPrint and Moonraker services.
Bambu printers do not advertise via mDNS and must be configured
manually.
"""

from __future__ import annotations

import socket
from dataclasses import asdict, dataclass
from typing import Any, Dict, List

try:
    from zeroconf import ServiceBrowser, ServiceInfo, Zeroconf

    ZEROCONF_AVAILABLE = True
except ImportError:
    ZEROCONF_AVAILABLE = False


@dataclass
class DiscoveredPrinter:
    """A printer found on the local network."""

    name: str
    printer_type: str  # "octoprint" or "moonraker"
    host: str
    port: int

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# mDNS service types to scan
_SERVICE_MAP = {
    "_octoprint._tcp.local.": "octoprint",
    "_moonraker._tcp.local.": "moonraker",
}


class _Collector:
    """Zeroconf listener that collects discovered services."""

    def __init__(self) -> None:
        self.found: List[DiscoveredPrinter] = []

    def add_service(self, zc: Any, type_: str, name: str) -> None:
        info = zc.get_service_info(type_, name)
        if info is None:
            return
        self._record(info, type_)

    def update_service(self, zc: Any, type_: str, name: str) -> None:
        pass  # not needed for one-shot scan

    def remove_service(self, zc: Any, type_: str, name: str) -> None:
        pass

    def _record(self, info: Any, service_type: str) -> None:
        printer_type = _SERVICE_MAP.get(service_type, "unknown")

        # Extract IP address
        addresses = getattr(info, "parsed_addresses", None)
        if addresses:
            ip = addresses[0]
        elif info.addresses:
            ip = socket.inet_ntoa(info.addresses[0])
        else:
            return

        port = info.port or (80 if printer_type == "octoprint" else 7125)
        instance_name = info.name.replace(f".{service_type}", "").strip(".")

        host = f"http://{ip}:{port}" if port not in (80, 443) else f"http://{ip}"

        self.found.append(DiscoveredPrinter(
            name=instance_name,
            printer_type=printer_type,
            host=host,
            port=port,
        ))


def discover_printers(timeout: float = 3.0) -> List[DiscoveredPrinter]:
    """Scan the local network for 3D printers.

    Args:
        timeout: How long to scan in seconds (default 3).

    Returns:
        List of discovered printers (may be empty).

    Raises:
        RuntimeError: If ``zeroconf`` is not installed.
    """
    if not ZEROCONF_AVAILABLE:
        raise RuntimeError(
            "Network discovery requires the 'zeroconf' package.  "
            "Install it with: pip install zeroconf"
        )

    import time

    zc = Zeroconf()
    collector = _Collector()

    browsers = []
    for service_type in _SERVICE_MAP:
        browsers.append(ServiceBrowser(zc, service_type, collector))

    time.sleep(timeout)
    zc.close()

    # Deduplicate by host
    seen: set[str] = set()
    unique: List[DiscoveredPrinter] = []
    for p in collector.found:
        if p.host not in seen:
            seen.add(p.host)
            unique.append(p)

    return unique
