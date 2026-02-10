"""CLI discovery wrapper -- thin delegate to :mod:`kiln.discovery`.

All real discovery logic lives in ``kiln.discovery``.  This module
re-exports the public API so the CLI can ``from kiln.cli.discovery
import discover_printers`` without reaching into the core package
directly.
"""

from __future__ import annotations

from kiln.discovery import DiscoveredPrinter, discover_printers, probe_host

__all__ = ["DiscoveredPrinter", "discover_printers", "probe_host"]
