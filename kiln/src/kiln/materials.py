"""Multi-material and spool tracking for Kiln.

Tracks which filament material is loaded in each printer's extruder(s),
maintains a spool inventory, and provides mismatch detection for the
preflight check system.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class LoadedMaterial:
    """Material currently loaded in a printer's tool slot."""

    printer_name: str
    tool_index: int = 0
    material_type: str = "unknown"
    color: str | None = None
    spool_id: str | None = None
    loaded_at: float = field(default_factory=time.time)
    remaining_grams: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Spool:
    """A spool in the filament inventory."""

    id: str
    material_type: str
    color: str | None = None
    brand: str | None = None
    weight_grams: float = 1000.0
    remaining_grams: float = 1000.0
    cost_usd: float | None = None
    purchase_date: float | None = None
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MaterialWarning:
    """Warning produced when loaded material does not match expected."""

    printer_name: str
    expected: str
    loaded: str
    severity: str = "warning"
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Material tracker
# ---------------------------------------------------------------------------


class MaterialTracker:
    """Thread-safe material and spool tracking.

    Parameters:
        db: Optional :class:`~kiln.persistence.KilnDB` instance.
            When provided, material and spool state is persisted.
        event_bus: Optional :class:`~kiln.events.EventBus` for publishing
            material-related events.
    """

    def __init__(self, db: Any = None, event_bus: Any = None) -> None:
        self._db = db
        self._bus = event_bus
        self._lock = threading.Lock()

    # -- material operations --------------------------------------------

    def set_material(
        self,
        printer_name: str,
        material_type: str,
        color: str | None = None,
        spool_id: str | None = None,
        tool_index: int = 0,
        remaining_grams: float | None = None,
    ) -> LoadedMaterial:
        """Record which material is loaded in a printer tool slot."""
        with self._lock:
            mat = LoadedMaterial(
                printer_name=printer_name,
                tool_index=tool_index,
                material_type=material_type.upper(),
                color=color,
                spool_id=spool_id,
                loaded_at=time.time(),
                remaining_grams=remaining_grams,
            )
            if self._db is not None:
                self._db.save_material(
                    printer_name=printer_name,
                    tool_index=tool_index,
                    material_type=mat.material_type,
                    color=color,
                    spool_id=spool_id,
                    remaining_grams=remaining_grams,
                )
            if self._bus is not None:
                from kiln.events import EventType

                self._bus.publish(
                    EventType.MATERIAL_LOADED,
                    data=mat.to_dict(),
                    source=f"printer:{printer_name}",
                )
            return mat

    def get_material(
        self,
        printer_name: str,
        tool_index: int = 0,
    ) -> LoadedMaterial | None:
        """Get the material loaded in a specific tool slot."""
        if self._db is None:
            return None
        row = self._db.get_material(printer_name, tool_index)
        if row is None:
            return None
        return LoadedMaterial(**{k: row[k] for k in LoadedMaterial.__dataclass_fields__ if k in row})

    def get_all_materials(self, printer_name: str) -> list[LoadedMaterial]:
        """Get all loaded materials for a printer."""
        if self._db is None:
            return []
        rows = self._db.list_materials(printer_name)
        results: list[LoadedMaterial] = []
        for row in rows:
            results.append(LoadedMaterial(**{k: row[k] for k in LoadedMaterial.__dataclass_fields__ if k in row}))
        return results

    def check_match(
        self,
        printer_name: str,
        expected_material: str,
        tool_index: int = 0,
    ) -> MaterialWarning | None:
        """Check if the loaded material matches what's expected.

        Returns a :class:`MaterialWarning` if there is a mismatch,
        or ``None`` if the materials match (or no material is loaded).
        """
        loaded = self.get_material(printer_name, tool_index)
        if loaded is None:
            return None

        expected_upper = expected_material.upper()
        loaded_upper = loaded.material_type.upper()

        if expected_upper == loaded_upper:
            return None

        warning = MaterialWarning(
            printer_name=printer_name,
            expected=expected_upper,
            loaded=loaded_upper,
            severity="warning",
            message=(
                f"Material mismatch on {printer_name} tool {tool_index}: "
                f"expected {expected_upper}, loaded {loaded_upper}"
            ),
        )
        if self._bus is not None:
            from kiln.events import EventType

            self._bus.publish(
                EventType.MATERIAL_MISMATCH,
                data=warning.to_dict(),
                source=f"printer:{printer_name}",
            )
        return warning

    def deduct_usage(
        self,
        printer_name: str,
        grams: float,
        tool_index: int = 0,
    ) -> float | None:
        """Subtract used grams from loaded material and linked spool.

        Returns the new remaining grams, or ``None`` if no material tracked.
        Emits SPOOL_LOW when remaining drops below 10% of spool weight,
        and SPOOL_EMPTY when remaining reaches zero.
        """
        # Collect event data inside the lock, emit after release
        _spool_warning_args = None

        with self._lock:
            if self._db is None:
                return None
            row = self._db.get_material(printer_name, tool_index)
            if row is None:
                return None

            old_remaining = row.get("remaining_grams")
            if old_remaining is None:
                return None

            new_remaining = max(0.0, old_remaining - grams)
            self._db.update_material_remaining(
                printer_name,
                tool_index,
                new_remaining,
            )

            # Also deduct from linked spool
            spool_id = row.get("spool_id")
            if spool_id and self._db is not None:
                spool_row = self._db.get_spool(spool_id)
                if spool_row:
                    spool_remaining = max(
                        0.0,
                        spool_row["remaining_grams"] - grams,
                    )
                    self._db.update_spool_remaining(spool_id, spool_remaining)
                    _spool_warning_args = (
                        spool_id,
                        spool_remaining,
                        spool_row["weight_grams"],
                        printer_name,
                    )

        # Emit events outside the lock to prevent deadlocks
        if _spool_warning_args is not None:
            self._emit_spool_warnings(*_spool_warning_args)

        return new_remaining

    def _emit_spool_warnings(
        self,
        spool_id: str,
        remaining: float,
        total: float,
        printer_name: str,
    ) -> None:
        """Emit spool low/empty events if thresholds are crossed."""
        if self._bus is None:
            return
        from kiln.events import EventType

        if remaining <= 0:
            self._bus.publish(
                EventType.SPOOL_EMPTY,
                data={"spool_id": spool_id, "printer_name": printer_name},
                source=f"printer:{printer_name}",
            )
        elif total > 0 and (remaining / total) < 0.10:
            self._bus.publish(
                EventType.SPOOL_LOW,
                data={
                    "spool_id": spool_id,
                    "remaining_grams": remaining,
                    "percent": round((remaining / total) * 100, 1),
                    "printer_name": printer_name,
                },
                source=f"printer:{printer_name}",
            )

    # -- spool operations -----------------------------------------------

    def add_spool(
        self,
        material_type: str,
        color: str | None = None,
        brand: str | None = None,
        weight_grams: float = 1000.0,
        cost_usd: float | None = None,
        notes: str = "",
    ) -> Spool:
        """Add a new spool to inventory."""
        spool_id = os.urandom(6).hex()
        spool = Spool(
            id=spool_id,
            material_type=material_type.upper(),
            color=color,
            brand=brand,
            weight_grams=weight_grams,
            remaining_grams=weight_grams,
            cost_usd=cost_usd,
            purchase_date=time.time(),
            notes=notes,
        )
        if self._db is not None:
            self._db.save_spool(spool.to_dict())
        return spool

    def remove_spool(self, spool_id: str) -> bool:
        """Remove a spool from inventory."""
        if self._db is None:
            return False
        return self._db.remove_spool(spool_id)

    def list_spools(self) -> list[Spool]:
        """Return all spools in inventory."""
        if self._db is None:
            return []
        rows = self._db.list_spools()
        return [Spool(**{k: row[k] for k in Spool.__dataclass_fields__ if k in row}) for row in rows]

    def get_spool(self, spool_id: str) -> Spool | None:
        """Get a spool by ID."""
        if self._db is None:
            return None
        row = self._db.get_spool(spool_id)
        if row is None:
            return None
        return Spool(**{k: row[k] for k in Spool.__dataclass_fields__ if k in row})
