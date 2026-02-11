"""Printer registry — manages multiple printer backends by name.

The registry is the single source of truth for all configured printers.
Agents interact with printers by name (e.g. ``"voron-350"``, ``"ender-farm-1"``)
rather than managing connection details directly.

Example::

    registry = PrinterRegistry()
    registry.register("voron", OctoPrintAdapter("http://voron.local", "KEY"))
    registry.register("ender", MoonrakerAdapter("http://ender.local"))

    state = registry.get("voron").get_state()
    all_idle = registry.get_idle_printers()
"""

from __future__ import annotations

import logging
import threading
from typing import Dict, List, Optional

from kiln.printers.base import PrinterAdapter, PrinterState, PrinterStatus

logger = logging.getLogger(__name__)


class PrinterNotFoundError(KeyError):
    """Raised when a printer name is not in the registry."""

    def __init__(self, name: str) -> None:
        super().__init__(f"Printer not found: {name!r}")
        self.printer_name = name


class PrinterRegistry:
    """Thread-safe registry of named printer adapters.

    All access is serialised via a lock so the registry can be safely
    queried from MCP tool handlers running on different threads.
    """

    def __init__(self) -> None:
        self._printers: Dict[str, PrinterAdapter] = {}
        self._lock = threading.Lock()
        self._printer_locks: Dict[str, threading.Lock] = {}

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self, name: str, adapter: PrinterAdapter) -> None:
        """Add or replace a printer in the registry.

        Args:
            name: Unique human-readable name for this printer.
            adapter: A fully-configured :class:`PrinterAdapter` instance.
        """
        with self._lock:
            self._printers[name] = adapter
            if name not in self._printer_locks:
                self._printer_locks[name] = threading.Lock()
            logger.info("Registered printer %r (%s)", name, adapter.name)

    def unregister(self, name: str) -> None:
        """Remove a printer from the registry.

        Raises:
            PrinterNotFoundError: If *name* is not registered.
        """
        with self._lock:
            if name not in self._printers:
                raise PrinterNotFoundError(name)
            del self._printers[name]
            logger.info("Unregistered printer %r", name)

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    def get(self, name: str) -> PrinterAdapter:
        """Return the adapter for *name*.

        Raises:
            PrinterNotFoundError: If *name* is not registered.
        """
        with self._lock:
            if name not in self._printers:
                raise PrinterNotFoundError(name)
            return self._printers[name]

    def list_names(self) -> List[str]:
        """Return a sorted list of all registered printer names."""
        with self._lock:
            return sorted(self._printers.keys())

    def list_all(self) -> Dict[str, PrinterAdapter]:
        """Return a shallow copy of the full name→adapter mapping."""
        with self._lock:
            return dict(self._printers)

    @property
    def count(self) -> int:
        """Number of registered printers."""
        with self._lock:
            return len(self._printers)

    def __contains__(self, name: str) -> bool:
        with self._lock:
            return name in self._printers

    # ------------------------------------------------------------------
    # Fleet queries
    # ------------------------------------------------------------------

    def get_fleet_status(self) -> List[Dict]:
        """Query every printer and return a list of status snapshots.

        Each entry contains the printer name, backend type, and current
        state.  Printers that fail to respond are reported as OFFLINE
        rather than raising.
        """
        results: List[Dict] = []
        printers = self.list_all()

        for name, adapter in printers.items():
            try:
                state = adapter.get_state()
                results.append({
                    "name": name,
                    "backend": adapter.name,
                    "connected": state.connected,
                    "state": state.state.value,
                    "tool_temp_actual": state.tool_temp_actual,
                    "tool_temp_target": state.tool_temp_target,
                    "bed_temp_actual": state.bed_temp_actual,
                    "bed_temp_target": state.bed_temp_target,
                })
            except Exception as exc:
                logger.warning("Failed to query printer %r: %s", name, exc)
                results.append({
                    "name": name,
                    "backend": adapter.name,
                    "connected": False,
                    "state": PrinterStatus.OFFLINE.value,
                    "tool_temp_actual": None,
                    "tool_temp_target": None,
                    "bed_temp_actual": None,
                    "bed_temp_target": None,
                })

        return results

    def get_idle_printers(self) -> List[str]:
        """Return names of printers that are currently idle and ready.

        Useful for job scheduling — find a printer that can accept work.
        """
        idle: List[str] = []
        printers = self.list_all()

        for name, adapter in printers.items():
            try:
                state = adapter.get_state()
                if state.connected and state.state == PrinterStatus.IDLE:
                    idle.append(name)
            except Exception:
                continue

        return sorted(idle)

    def get_printers_by_status(self, status: PrinterStatus) -> List[str]:
        """Return names of printers in the given state."""
        matched: List[str] = []
        printers = self.list_all()

        for name, adapter in printers.items():
            try:
                state = adapter.get_state()
                if state.state == status:
                    matched.append(name)
            except Exception:
                if status == PrinterStatus.OFFLINE:
                    matched.append(name)

        return sorted(matched)

    # ------------------------------------------------------------------
    # Per-printer mutex
    # ------------------------------------------------------------------

    def printer_lock(self, name: str) -> threading.Lock:
        """Return the per-printer lock for exclusive operations.

        Use this to prevent concurrent agents from controlling the same
        printer simultaneously (e.g. uploading files or starting prints).

        Raises:
            PrinterNotFoundError: If *name* is not registered.
        """
        with self._lock:
            if name not in self._printers:
                raise PrinterNotFoundError(name)
            if name not in self._printer_locks:
                self._printer_locks[name] = threading.Lock()
            return self._printer_locks[name]
