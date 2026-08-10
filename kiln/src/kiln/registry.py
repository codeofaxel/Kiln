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
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, TypeVar

from kiln.printers.base import PrinterAdapter, PrinterStatus

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

# Per-printer timeout for fleet queries (seconds).
_FLEET_QUERY_TIMEOUT: float = 10.0


@dataclass
class PrinterMetadata:
    """Metadata for a registered printer.

    :param site: Physical site/location name (e.g. ``"Building A"``).
    :param tags: Arbitrary key-value tags for filtering.
    :param registered_at: Unix timestamp of registration.
    """

    site: str = ""
    tags: dict[str, str] = field(default_factory=dict)
    registered_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "site": self.site,
            "tags": dict(self.tags),
            "registered_at": self.registered_at,
        }


def _normalize_host(host: str) -> str:
    """Reduce a printer address to a comparable form.

    ``http://192.168.1.6:7125/`` and ``192.168.1.6:7125`` address the same
    machine.  The port is KEPT — two print servers on one box (two
    Moonraker instances behind one Pi) are genuinely two printers.
    """
    text = str(host or "").strip().lower()
    for scheme in ("https://", "http://"):
        if text.startswith(scheme):
            text = text[len(scheme):]
            break
    return text.rstrip("/")


def machine_fingerprint(adapter: PrinterAdapter) -> str:
    """Return a stable identity for the physical machine behind *adapter*.

    Names are labels, not identity: the server registers the active
    printer as ``"default"`` AND again under its config.yaml name, so one
    machine occupied two registry slots and counted twice.  That inflated
    ``printer_count`` telemetry, showed the user their printer twice, and
    corrupted the tier cap's arithmetic — a single-printer free user
    already "used" two of one allowed slot (2026-07-27).

    Serial wins when the backend reports one (Bambu): it survives a DHCP
    lease change.  Otherwise the normalized address identifies the box.
    An adapter that exposes neither is unidentifiable, so it falls back to
    its own object identity — distinct by definition, never merged with
    another machine on a guess.
    """
    family = str(getattr(adapter, "name", "") or type(adapter).__name__).lower()
    serial = str(
        getattr(adapter, "serial", None) or getattr(adapter, "_serial", None) or ""
    ).strip()
    if serial:
        return f"{family}:serial:{serial.lower()}"
    host = _normalize_host(
        getattr(adapter, "host", None) or getattr(adapter, "_host", None) or ""
    )
    if host:
        return f"{family}:host:{host}"
    return f"{family}:object:{id(adapter):x}"


def _disconnect_adapter(adapter: PrinterAdapter, name: str = "") -> None:
    """Safely disconnect an adapter if it supports it.

    Not all backends have persistent connections (e.g. OctoPrint and
    Moonraker use stateless HTTP), so ``disconnect()`` is optional.
    """
    disconnect_fn = getattr(adapter, "disconnect", None)
    if disconnect_fn is None:
        return
    try:
        logger.info("Disconnecting old adapter for %r", name)
        disconnect_fn()
    except Exception as exc:
        logger.warning("Failed to disconnect adapter for %r: %s", name, exc)


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
        self._printers: dict[str, PrinterAdapter] = {}
        self._metadata: dict[str, PrinterMetadata] = {}
        self._lock = threading.Lock()
        self._printer_locks: dict[str, threading.Lock] = {}

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(
        self,
        name: str,
        adapter: PrinterAdapter,
        *,
        site: str = "",
        tags: dict[str, str] | None = None,
    ) -> None:
        """Add or replace a printer in the registry.

        :param name: Unique human-readable name for this printer.
        :param adapter: A fully-configured :class:`PrinterAdapter` instance.
        :param site: Physical site/location name (e.g. ``"Building A"``).
        :param tags: Arbitrary key-value tags for filtering.
        """
        with self._lock:
            # Grab the old adapter if replacing an existing printer.
            old_adapter = self._printers.get(name)
            if old_adapter is adapter:
                old_adapter = None  # Same object, no disconnect needed.
            self._printers[name] = adapter
            self._metadata[name] = PrinterMetadata(
                site=site,
                tags=dict(tags) if tags else {},
            )
            if name not in self._printer_locks:
                self._printer_locks[name] = threading.Lock()
            logger.info("Registered printer %r (%s) at site %r", name, adapter.name, site)
        # Disconnect outside the lock to avoid holding it during I/O
        # (MQTT disconnect can block waiting for thread join).
        if old_adapter is not None:
            _disconnect_adapter(old_adapter, name)

    def unregister(self, name: str) -> None:
        """Remove a printer from the registry.

        Disconnects the adapter before removing it so MQTT/websocket
        threads are cleanly stopped.

        Raises:
            PrinterNotFoundError: If *name* is not registered.
        """
        with self._lock:
            if name not in self._printers:
                raise PrinterNotFoundError(name)
            adapter = self._printers.pop(name)
            self._metadata.pop(name, None)
            logger.info("Unregistered printer %r", name)
        # Disconnect outside the lock to avoid holding it during I/O.
        _disconnect_adapter(adapter, name)

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

    def list_names(self) -> list[str]:
        """Return a sorted list of all registered printer names."""
        with self._lock:
            return sorted(self._printers.keys())

    def list_all(self) -> dict[str, PrinterAdapter]:
        """Return a shallow copy of the full name->adapter mapping."""
        with self._lock:
            return dict(self._printers)

    @property
    def count(self) -> int:
        """Number of distinct physical MACHINES registered.

        Counts machines, not names — two names for one printer (the
        ``"default"`` alias the server registers alongside the config.yaml
        name) is one machine.  Everything that asks "how many printers does
        this install have" — the tier cap, the usage heartbeat, the startup
        bootstrap — means machines, and got a name count until 2026-07-27.
        """
        with self._lock:
            return len({
                machine_fingerprint(a) for a in self._printers.values()
            })

    @property
    def name_count(self) -> int:
        """Number of registered NAMES, aliases included.

        The bookkeeping view — use :attr:`count` for anything a user or a
        price is measured against.
        """
        with self._lock:
            return len(self._printers)

    def list_machines(self) -> list[str]:
        """Return one canonical name per distinct machine, sorted.

        The name a user chose beats the ``"default"`` alias the bootstrap
        adds, so a fleet view shows "my-voron", not the same printer twice.
        """
        with self._lock:
            by_machine: dict[str, str] = {}
            for name in sorted(self._printers):
                fp = machine_fingerprint(self._printers[name])
                current = by_machine.get(fp)
                if current is None or (current == "default" and name != "default"):
                    by_machine[fp] = name
            return sorted(by_machine.values())

    def aliases_of(self, name: str) -> list[str]:
        """Return every registered name pointing at *name*'s machine."""
        with self._lock:
            adapter = self._printers.get(name)
            if adapter is None:
                return []
            target = machine_fingerprint(adapter)
            return sorted(
                other for other, a in self._printers.items()
                if machine_fingerprint(a) == target
            )

    def __contains__(self, name: str) -> bool:
        with self._lock:
            return name in self._printers

    # ------------------------------------------------------------------
    # Parallel fleet helpers
    # ------------------------------------------------------------------

    def _query_printers_parallel(
        self,
        printers: dict[str, PrinterAdapter],
        query_fn: Callable[[str, PrinterAdapter], _T],
        error_fn: Callable[[str, PrinterAdapter, Exception], _T],
    ) -> list[_T]:
        """Query all printers in parallel using a thread pool.

        Args:
            printers: Name-to-adapter mapping to query.
            query_fn: Called with (name, adapter) for each printer.
                Must return a result of type *_T*.
            error_fn: Called with (name, adapter, exception) when *query_fn*
                raises.  Must return a fallback result of type *_T*.

        Returns:
            A list of results, one per printer (order not guaranteed).
        """
        if not printers:
            return []

        max_workers = min(len(printers), 20)
        results: list[_T] = []

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            future_to_name = {
                pool.submit(query_fn, name, adapter): (name, adapter) for name, adapter in printers.items()
            }
            for future in as_completed(future_to_name, timeout=_FLEET_QUERY_TIMEOUT + 5):
                name, adapter = future_to_name[future]
                try:
                    results.append(future.result(timeout=_FLEET_QUERY_TIMEOUT))
                except Exception as exc:
                    results.append(error_fn(name, adapter, exc))

        return results

    # ------------------------------------------------------------------
    # Fleet queries
    # ------------------------------------------------------------------

    def get_fleet_status(self) -> list[dict]:
        """Query every printer and return a list of status snapshots.

        Each entry contains the printer name, backend type, and current
        state.  Printers that fail to respond are reported as OFFLINE
        rather than raising.

        One row per MACHINE, not per name — the bootstrap's ``"default"``
        alias would otherwise list the user's single printer twice, and
        double every parallel query against it.

        Queries are executed in parallel for speed.
        """
        canonical = set(self.list_machines())
        printers = {
            n: a for n, a in self.list_all().items() if n in canonical
        }
        with self._lock:
            metadata_snapshot = dict(self._metadata)

        def _site_for(name: str) -> str:
            meta = metadata_snapshot.get(name)
            return meta.site if meta else ""

        def _model_for(adapter: PrinterAdapter) -> str | None:
            # Raw model string (e.g. "bambu_a1") so clients can resolve a
            # display label and build volume. Not every adapter tracks one.
            # One resolver shared with the heartbeat and community paths:
            # live self-report (get_printer_info) first, config-fed
            # attributes second — the fleet view must never disagree with
            # the telemetry about what a printer is.
            try:
                from kiln.community_autofire import resolve_adapter_model

                return resolve_adapter_model(adapter)
            except Exception:
                m = getattr(adapter, "printer_model", None) or getattr(adapter, "_printer_model", None)
                m = (m or "").strip()
                return m or None

        def _query(name: str, adapter: PrinterAdapter) -> dict:
            state = adapter.get_state()
            return {
                "name": name,
                "backend": adapter.name,
                "model": _model_for(adapter),
                "site": _site_for(name),
                "connected": state.connected,
                "state": state.state.value,
                "tool_temp_actual": state.tool_temp_actual,
                "tool_temp_target": state.tool_temp_target,
                "bed_temp_actual": state.bed_temp_actual,
                "bed_temp_target": state.bed_temp_target,
                # The nozzle the printer itself reports, when the adapter
                # surfaces it (Bambu populates these from its MQTT report;
                # adapters that don't leave them None). Carried per-printer so a
                # fleet view can cross-check each printer's installed nozzle
                # against its saved record — not just the one active printer
                # printer_status() happens to reflect.
                "nozzle_type": state.nozzle_type,
                "nozzle_diameter": state.nozzle_diameter,
            }

        def _error(name: str, adapter: PrinterAdapter, exc: Exception) -> dict:
            logger.warning("Failed to query printer %r: %s", name, exc)
            return {
                "name": name,
                "backend": adapter.name,
                "model": _model_for(adapter),
                "site": _site_for(name),
                "connected": False,
                "state": PrinterStatus.OFFLINE.value,
                "tool_temp_actual": None,
                "tool_temp_target": None,
                "bed_temp_actual": None,
                "bed_temp_target": None,
            }

        return self._query_printers_parallel(printers, _query, _error)

    def get_idle_printers(self) -> list[str]:
        """Return names of printers that are currently idle and ready.

        Useful for job scheduling -- find a printer that can accept work.
        Queries are executed in parallel for speed.
        """
        printers = self.list_all()

        def _query(name: str, adapter: PrinterAdapter) -> tuple[str, bool]:
            state = adapter.get_state()
            return (name, state.connected and state.state == PrinterStatus.IDLE)

        def _error(name: str, adapter: PrinterAdapter, exc: Exception) -> tuple[str, bool]:
            return (name, False)

        results = self._query_printers_parallel(printers, _query, _error)
        return sorted(name for name, is_idle in results if is_idle)

    def get_printers_by_status(self, status: PrinterStatus) -> list[str]:
        """Return names of printers in the given state.

        Queries are executed in parallel for speed.
        """
        printers = self.list_all()

        def _query(name: str, adapter: PrinterAdapter) -> tuple[str, bool]:
            state = adapter.get_state()
            return (name, state.state == status)

        def _error(name: str, adapter: PrinterAdapter, exc: Exception) -> tuple[str, bool]:
            # Printers that fail to respond match OFFLINE queries.
            return (name, status == PrinterStatus.OFFLINE)

        results = self._query_printers_parallel(printers, _query, _error)
        return sorted(name for name, matched in results if matched)

    # ------------------------------------------------------------------
    # Site / metadata queries
    # ------------------------------------------------------------------

    def get_metadata(self, name: str) -> PrinterMetadata:
        """Return metadata for a printer.

        :raises PrinterNotFoundError: If *name* is not registered.
        """
        with self._lock:
            if name not in self._printers:
                raise PrinterNotFoundError(name)
            return self._metadata[name]

    def list_sites(self) -> list[str]:
        """Return sorted list of unique site names (excluding empty)."""
        with self._lock:
            return sorted({m.site for m in self._metadata.values() if m.site})

    def get_printers_by_site(self, site: str) -> list[str]:
        """Return sorted printer names at a given site."""
        with self._lock:
            return sorted(
                name for name, meta in self._metadata.items() if meta.site == site
            )

    def get_fleet_status_by_site(self) -> dict[str, list[dict]]:
        """Query all printers and group results by site.

        Returns a dict mapping site name to a list of printer status dicts.
        Printers with no site are grouped under ``"unassigned"``.
        """
        statuses = self.get_fleet_status()
        grouped: dict[str, list[dict]] = {}
        for entry in statuses:
            site_key = entry.get("site") or "unassigned"
            grouped.setdefault(site_key, []).append(entry)
        return grouped

    def update_printer_metadata(
        self,
        name: str,
        *,
        site: str | None = None,
        tags: dict[str, str] | None = None,
    ) -> PrinterMetadata:
        """Update metadata for a registered printer.

        Only the provided fields are changed; others are left intact.

        :param name: Printer name.
        :param site: New site name, or *None* to leave unchanged.
        :param tags: New tags dict, or *None* to leave unchanged.
        :raises PrinterNotFoundError: If *name* is not registered.
        """
        with self._lock:
            if name not in self._printers:
                raise PrinterNotFoundError(name)
            meta = self._metadata[name]
            if site is not None:
                meta.site = site
            if tags is not None:
                meta.tags = dict(tags)
            return meta

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


# ---------------------------------------------------------------------------
# Module-level singleton accessor
# ---------------------------------------------------------------------------

_registry_singleton: PrinterRegistry | None = None
_registry_singleton_lock = threading.Lock()


def get_printer_registry() -> PrinterRegistry:
    """Return the lazily-initialised module-level :class:`PrinterRegistry`.

    Modules outside ``kiln.server`` (notably ``print_health_monitor``,
    ``heartbeat``, ``kiln_pro.recovery.auto_recover_engine``) reach for
    a registry without a circular dependency on ``kiln.server``.  This
    accessor is the canonical no-circular import path; ``kiln.server``
    has its own ``_get_registry`` that wraps the same singleton via
    ``register_default_singleton`` so both surfaces converge.

    Thread-safe via double-checked locking.  Returns the same instance
    on every call.
    """
    global _registry_singleton
    if _registry_singleton is not None:
        return _registry_singleton
    with _registry_singleton_lock:
        if _registry_singleton is None:
            _registry_singleton = PrinterRegistry()
        return _registry_singleton


def register_default_singleton(registry: PrinterRegistry) -> None:
    """Replace the module-level singleton.

    ``kiln.server._get_registry`` calls this on first registry build
    so that ``get_printer_registry`` returns the same instance the
    server has populated with adapters.  Without this convergence,
    callers that imported ``get_printer_registry`` directly would
    see an empty registry while the server has the real one.
    """
    global _registry_singleton
    with _registry_singleton_lock:
        _registry_singleton = registry


# Backwards-compat alias used by ``kiln.heartbeat``.  Kept as a thin
# alias rather than a duplicate function so a future rename only
# touches one body.
get_registry = get_printer_registry
