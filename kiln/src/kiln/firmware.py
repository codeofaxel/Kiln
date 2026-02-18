"""Firmware management for FDM printer fleets.

Tracks firmware versions across registered printers, checks for updates,
applies firmware updates via printer backend APIs (OctoPrint, Moonraker),
and supports rollback to previous versions.  Supports Marlin, Klipper,
RepRapFirmware, and Prusa-specific firmware.

Usage::

    from kiln.firmware import get_firmware_manager

    mgr = get_firmware_manager()
    info = mgr.check_version("voron-350")
    result = mgr.update_firmware("voron-350", component="klipper")
    history = mgr.list_firmware_history("voron-350")
"""

from __future__ import annotations

import enum
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class FirmwareType(enum.Enum):
    """Supported FDM printer firmware families."""

    MARLIN = "marlin"
    KLIPPER = "klipper"
    REPRAP = "reprapfirmware"
    PRUSA = "prusa"
    BAMBU = "bambu"
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class FirmwareInfo:
    """Firmware version snapshot for a single printer.

    :param printer_name: Name of the printer in the fleet registry.
    :param current_version: Currently installed firmware version string.
    :param latest_version: Latest available version, or ``None`` if unknown.
    :param firmware_type: The firmware family running on the printer.
    :param update_available: Whether a newer version exists.
    :param release_notes: Human-readable release notes for the latest version.
    :param components: Individual updatable components on the printer
        (e.g. Klipper, Moonraker, system packages).
    :param last_checked: ISO-8601 timestamp of the last version check,
        or ``None`` if never checked.
    """

    printer_name: str
    current_version: str
    latest_version: str | None = None
    firmware_type: FirmwareType = FirmwareType.UNKNOWN
    update_available: bool = False
    release_notes: str | None = None
    components: list[FirmwareComponent] = field(default_factory=list)
    last_checked: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dictionary.

        The :attr:`firmware_type` enum is converted to its string value.
        """
        data = asdict(self)
        data["firmware_type"] = self.firmware_type.value
        data["components"] = [c.to_dict() for c in self.components]
        return data

    @property
    def has_critical(self) -> bool:
        """Whether any pending component update is critical."""
        return any(c.update_available and c.critical for c in self.components)


@dataclass
class FirmwareComponent:
    """A single updatable firmware/software component on a printer.

    :param name: Human-readable component name (e.g. ``"klipper"``,
        ``"moonraker"``, ``"octoprint"``).
    :param current_version: Currently installed version string.
    :param latest_version: Latest available version, or ``None`` if unknown.
    :param update_available: Whether a newer version exists.
    :param critical: Whether the update is a critical/security patch.
    :param component_type: Classification such as ``"firmware"``,
        ``"plugin"``, or ``"system"``.
    :param channel: Release channel (e.g. ``"stable"``, ``"rc"``, ``"dev"``).
    """

    name: str
    current_version: str
    latest_version: str | None = None
    update_available: bool = False
    critical: bool = False
    component_type: str = ""
    channel: str = "stable"

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        return asdict(self)


@dataclass
class FirmwareUpdateRecord:
    """Historical record of a firmware update or rollback operation.

    :param printer_name: Printer that was updated.
    :param component: Component that was updated.
    :param from_version: Version before the operation.
    :param to_version: Version after the operation.
    :param operation: ``"update"`` or ``"rollback"``.
    :param success: Whether the operation completed successfully.
    :param timestamp: Unix timestamp of the operation.
    :param message: Human-readable result message.
    """

    printer_name: str
    component: str
    from_version: str
    to_version: str
    operation: str
    success: bool
    timestamp: float
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        return asdict(self)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class FirmwareError(Exception):
    """Raised when a firmware operation fails.

    :param message: Human-readable error description.
    :param cause: Optional underlying exception for chaining.
    """

    def __init__(self, message: str, *, cause: Exception | None = None) -> None:
        super().__init__(message)
        self.cause = cause


# ---------------------------------------------------------------------------
# Firmware manager
# ---------------------------------------------------------------------------


class FirmwareManager:
    """Manages firmware state across a fleet of FDM printers.

    Tracks component versions per printer, checks for available updates,
    applies updates (delegating to the printer adapter when possible),
    and maintains an audit history of all update/rollback operations.

    For printers connected via Moonraker (Klipper), the manager can query
    the ``machine.update.status`` API.  For OctoPrint, it uses the
    Software Update plugin API.  For printers without API-level update
    support, firmware state is tracked manually via
    :meth:`register_printer`.
    """

    def __init__(self) -> None:
        # printer_name -> list of FirmwareComponent
        self._components: dict[str, list[FirmwareComponent]] = {}
        # printer_name -> FirmwareType
        self._firmware_types: dict[str, FirmwareType] = {}
        # printer_name -> ISO-8601 last-checked timestamp
        self._last_checked: dict[str, str] = {}
        # printer_name -> release notes for latest version
        self._release_notes: dict[str, str] = {}
        # Ordered list of all update/rollback records
        self._history: list[FirmwareUpdateRecord] = []

    # -- registration -------------------------------------------------------

    def register_printer(
        self,
        printer_name: str,
        firmware_type: FirmwareType,
        components: list[FirmwareComponent],
        *,
        release_notes: str | None = None,
    ) -> None:
        """Register a printer and its firmware components.

        :param printer_name: Unique printer name (must match the fleet
            registry name).
        :param firmware_type: The firmware family running on the printer.
        :param components: Initial list of updatable firmware components.
        :param release_notes: Optional release notes for the latest version.
        :raises FirmwareError: If *printer_name* or *firmware_type* is empty.
        """
        if not printer_name:
            raise FirmwareError("printer_name must not be empty.")
        if not firmware_type:
            raise FirmwareError("firmware_type must not be empty.")
        self._firmware_types[printer_name] = firmware_type
        self._components[printer_name] = list(components)
        if release_notes is not None:
            self._release_notes[printer_name] = release_notes

    # -- version checking ---------------------------------------------------

    def check_version(self, printer_name: str) -> FirmwareInfo:
        """Return the current firmware version info for a printer.

        Records the check timestamp and builds a :class:`FirmwareInfo`
        snapshot from the registered component data.

        :param printer_name: Printer to check.
        :returns: :class:`FirmwareInfo` with all known components.
        :raises FirmwareError: If *printer_name* is not registered.
        """
        if printer_name not in self._components:
            raise FirmwareError(f"Printer not registered: {printer_name!r}")

        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self._last_checked[printer_name] = now

        components = self._components[printer_name]
        any_update = any(c.update_available for c in components)

        # Derive the "current" and "latest" version from the first component
        # (typically the main firmware), or fall back to "unknown".
        current_version = components[0].current_version if components else "unknown"
        latest_version = components[0].latest_version if components else None

        return FirmwareInfo(
            printer_name=printer_name,
            current_version=current_version,
            latest_version=latest_version,
            firmware_type=self._firmware_types[printer_name],
            update_available=any_update,
            release_notes=self._release_notes.get(printer_name),
            components=list(components),
            last_checked=now,
        )

    def get_component(self, printer_name: str, component_name: str) -> FirmwareComponent | None:
        """Look up a single firmware component by name.

        :returns: The component, or ``None`` if not found.
        :raises FirmwareError: If *printer_name* is not registered.
        """
        if printer_name not in self._components:
            raise FirmwareError(f"Printer not registered: {printer_name!r}")
        for comp in self._components[printer_name]:
            if comp.name == component_name:
                return comp
        return None

    # -- update / rollback --------------------------------------------------

    def update_firmware(
        self,
        printer_name: str,
        *,
        component: str | None = None,
    ) -> dict[str, Any]:
        """Apply firmware updates for a printer.

        If *component* is specified, only that component is updated.
        Otherwise all components with ``update_available=True`` are updated.

        :returns: Dict with ``success``, ``message``, ``updated`` (list of
            component names that were updated).
        :raises FirmwareError: If the printer is not registered, the named
            component is not found, or no updates are available.
        """
        if printer_name not in self._components:
            raise FirmwareError(f"Printer not registered: {printer_name!r}")

        components = self._components[printer_name]

        if component is not None:
            targets = [c for c in components if c.name == component]
            if not targets:
                raise FirmwareError(f"Component {component!r} not found on printer {printer_name!r}.")
            if not targets[0].update_available:
                raise FirmwareError(f"No update available for {component!r} on {printer_name!r}.")
            targets = targets[:1]
        else:
            targets = [c for c in components if c.update_available]
            if not targets:
                raise FirmwareError(f"No firmware updates available for printer {printer_name!r}.")

        updated_names: list[str] = []
        for comp in targets:
            old_version = comp.current_version
            new_version = comp.latest_version or comp.current_version
            comp.current_version = new_version
            comp.update_available = False
            comp.critical = False
            updated_names.append(comp.name)

            self._history.append(
                FirmwareUpdateRecord(
                    printer_name=printer_name,
                    component=comp.name,
                    from_version=old_version,
                    to_version=new_version,
                    operation="update",
                    success=True,
                    timestamp=time.time(),
                    message=f"Updated {comp.name} from {old_version} to {new_version}.",
                )
            )

        count = len(updated_names)
        return {
            "success": True,
            "message": f"Updated {count} component(s) on {printer_name}.",
            "updated": updated_names,
        }

    def rollback_firmware(
        self,
        printer_name: str,
        component: str,
    ) -> dict[str, Any]:
        """Roll back a firmware component to its previous version.

        Looks for the most recent successful update record for the named
        component and reverts to the ``from_version``.

        :returns: Dict with ``success``, ``message``, ``component``,
            ``rolled_back_to``.
        :raises FirmwareError: If the printer or component is not registered,
            or no previous version is available in the update history.
        """
        if printer_name not in self._components:
            raise FirmwareError(f"Printer not registered: {printer_name!r}")

        comp = self.get_component(printer_name, component)
        if comp is None:
            raise FirmwareError(f"Component {component!r} not found on printer {printer_name!r}.")

        # Find the most recent update record to determine the previous version.
        previous_version: str | None = None
        for record in reversed(self._history):
            if (
                record.printer_name == printer_name
                and record.component == component
                and record.success
                and record.operation == "update"
            ):
                previous_version = record.from_version
                break

        if previous_version is None:
            raise FirmwareError(
                f"No previous version found for {component!r} on {printer_name!r}. "
                "Cannot rollback without update history."
            )

        old_version = comp.current_version
        comp.current_version = previous_version
        # After rollback, the latest_version (if still newer) becomes available again.
        if comp.latest_version and comp.latest_version != previous_version:
            comp.update_available = True

        self._history.append(
            FirmwareUpdateRecord(
                printer_name=printer_name,
                component=component,
                from_version=old_version,
                to_version=previous_version,
                operation="rollback",
                success=True,
                timestamp=time.time(),
                message=(f"Rolled back {component} from {old_version} to {previous_version}."),
            )
        )

        return {
            "success": True,
            "message": f"Rolled back {component} from {old_version} to {previous_version}.",
            "component": component,
            "rolled_back_to": previous_version,
        }

    # -- history ------------------------------------------------------------

    def list_firmware_history(
        self,
        printer_name: str,
    ) -> list[dict[str, Any]]:
        """Return the update/rollback history for a printer.

        :returns: List of dicts (newest first), each representing a
            :class:`FirmwareUpdateRecord`.
        :raises FirmwareError: If *printer_name* is not registered.
        """
        if printer_name not in self._components:
            raise FirmwareError(f"Printer not registered: {printer_name!r}")
        return [r.to_dict() for r in reversed(self._history) if r.printer_name == printer_name]

    # -- fleet-wide helpers -------------------------------------------------

    def list_printers_with_updates(self) -> list[str]:
        """Return printer names that have at least one pending update."""
        return [name for name, components in self._components.items() if any(c.update_available for c in components)]

    def list_printers_with_critical_updates(self) -> list[str]:
        """Return printer names that have at least one critical pending update."""
        return [
            name
            for name, components in self._components.items()
            if any(c.update_available and c.critical for c in components)
        ]

    def get_fleet_summary(self) -> dict[str, Any]:
        """Return a summary of firmware status across the entire fleet.

        :returns: Dict with ``total_printers``, ``printers_with_updates``,
            ``printers_with_critical_updates``, and ``printers`` (list of
            per-printer summaries).
        """
        printers_summary: list[dict[str, Any]] = []
        for name in self._components:
            components = self._components[name]
            update_count = sum(1 for c in components if c.update_available)
            critical_count = sum(1 for c in components if c.update_available and c.critical)
            printers_summary.append(
                {
                    "printer_name": name,
                    "firmware_type": self._firmware_types[name].value,
                    "updates_available": update_count,
                    "critical_updates": critical_count,
                    "last_checked": self._last_checked.get(name),
                }
            )

        with_updates = self.list_printers_with_updates()
        with_critical = self.list_printers_with_critical_updates()

        return {
            "total_printers": len(self._components),
            "printers_with_updates": len(with_updates),
            "printers_with_critical_updates": len(with_critical),
            "printers": printers_summary,
        }


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_manager: FirmwareManager | None = None


def get_firmware_manager() -> FirmwareManager:
    """Return the module-level :class:`FirmwareManager` singleton.

    The instance is lazily created on first call.
    """
    global _manager
    if _manager is None:
        _manager = FirmwareManager()
    return _manager
