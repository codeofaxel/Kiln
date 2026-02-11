"""Bed leveling trigger system for Kiln.

Automatically triggers bed leveling (mesh probing) based on configurable
conditions: number of prints since last level, time elapsed, or manual
trigger.  Integrates with the event bus and persistence layer.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class LevelingPolicy:
    """Configuration for automatic bed leveling triggers."""

    enabled: bool = False
    max_prints_between_levels: int = 10
    max_hours_between_levels: float = 48.0
    temp_delta_trigger: float = 5.0
    auto_before_first_print: bool = True
    gcode_command: str = "G29"  # or "BED_MESH_CALIBRATE" for Klipper

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> LevelingPolicy:
        """Create a policy from a dict (e.g. from settings)."""
        return cls(**{
            k: data[k] for k in cls.__dataclass_fields__ if k in data
        })


@dataclass
class LevelingStatus:
    """Current bed leveling state for a printer."""

    printer_name: str
    last_leveled_at: Optional[float] = None
    prints_since_level: int = 0
    needs_leveling: bool = False
    trigger_reason: Optional[str] = None
    policy: Optional[LevelingPolicy] = None
    mesh_point_count: Optional[int] = None
    mesh_variance: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        if self.policy:
            d["policy"] = self.policy.to_dict()
        return d


# ---------------------------------------------------------------------------
# Bed level manager
# ---------------------------------------------------------------------------

class BedLevelManager:
    """Manages bed leveling policies and triggers.

    Parameters:
        db: Optional :class:`~kiln.persistence.KilnDB` for persisting
            leveling history.
        event_bus: Optional :class:`~kiln.events.EventBus` for event
            integration.
        registry: Optional :class:`~kiln.registry.PrinterRegistry` for
            looking up printers.
    """

    def __init__(
        self,
        db: Any = None,
        event_bus: Any = None,
        registry: Any = None,
    ) -> None:
        self._db = db
        self._bus = event_bus
        self._registry = registry
        self._lock = threading.Lock()
        # In-memory policy + counter storage (keyed by printer_name)
        self._policies: Dict[str, LevelingPolicy] = {}
        self._prints_since: Dict[str, int] = {}

    def subscribe_events(self) -> None:
        """Subscribe to job completion events on the event bus."""
        if self._bus is None:
            return
        from kiln.events import EventType
        self._bus.subscribe(EventType.JOB_COMPLETED, self._on_job_completed)

    def _on_job_completed(self, event: Any) -> None:
        """Handler for job completion events — increments print counter."""
        printer_name = event.data.get("printer_name")
        if not printer_name:
            return
        with self._lock:
            self._prints_since[printer_name] = (
                self._prints_since.get(printer_name, 0) + 1
            )
        # Check if leveling is now needed
        status = self.check_needed(printer_name)
        if status.needs_leveling and self._bus is not None:
            from kiln.events import EventType
            self._bus.publish(
                EventType.LEVELING_NEEDED,
                data=status.to_dict(),
                source=f"printer:{printer_name}",
            )

    # -- policy management ----------------------------------------------

    def set_policy(
        self, printer_name: str, policy: LevelingPolicy,
    ) -> None:
        """Set the leveling policy for a printer."""
        with self._lock:
            self._policies[printer_name] = policy
        # Persist via settings table
        if self._db is not None:
            import json
            self._db.set_setting(
                f"leveling_policy:{printer_name}",
                json.dumps(policy.to_dict()),
            )

    def get_policy(self, printer_name: str) -> LevelingPolicy:
        """Get the leveling policy for a printer (or default)."""
        with self._lock:
            cached = self._policies.get(printer_name)
            if cached is not None:
                return cached

        # Try loading from DB
        if self._db is not None:
            import json
            raw = self._db.get_setting(f"leveling_policy:{printer_name}")
            if raw:
                try:
                    policy = LevelingPolicy.from_dict(json.loads(raw))
                    with self._lock:
                        self._policies[printer_name] = policy
                    return policy
                except (json.JSONDecodeError, TypeError):
                    pass

        return LevelingPolicy()

    # -- status checks --------------------------------------------------

    def check_needed(self, printer_name: str) -> LevelingStatus:
        """Evaluate whether bed leveling is needed for a printer."""
        policy = self.get_policy(printer_name)
        last_record = None
        if self._db is not None:
            last_record = self._db.last_leveling(printer_name)

        last_leveled_at = last_record["started_at"] if last_record else None

        with self._lock:
            prints_since = self._prints_since.get(printer_name, 0)

        needs = False
        reason = None

        # Mesh stats if available
        mesh_points = None
        mesh_var = None
        if last_record and last_record.get("mesh_data"):
            mesh = last_record["mesh_data"]
            if isinstance(mesh, dict):
                points = mesh.get("probed_matrix") or mesh.get("points")
                if isinstance(points, list):
                    flat = []
                    for row in points:
                        if isinstance(row, list):
                            flat.extend(row)
                        else:
                            flat.append(row)
                    if flat:
                        mesh_points = len(flat)
                        mean = sum(flat) / len(flat)
                        mesh_var = sum((x - mean) ** 2 for x in flat) / len(flat)

        if not policy.enabled:
            return LevelingStatus(
                printer_name=printer_name,
                last_leveled_at=last_leveled_at,
                prints_since_level=prints_since,
                needs_leveling=False,
                policy=policy,
                mesh_point_count=mesh_points,
                mesh_variance=round(mesh_var, 6) if mesh_var is not None else None,
            )

        # Check first-print condition
        if policy.auto_before_first_print and last_leveled_at is None:
            needs = True
            reason = "No leveling history found (first print)"

        # Check prints threshold
        if (not needs
                and prints_since >= policy.max_prints_between_levels):
            needs = True
            reason = (
                f"Prints since last level ({prints_since}) >= "
                f"threshold ({policy.max_prints_between_levels})"
            )

        # Check time threshold
        if not needs and last_leveled_at is not None:
            hours_since = (time.time() - last_leveled_at) / 3600.0
            if hours_since >= policy.max_hours_between_levels:
                needs = True
                reason = (
                    f"Hours since last level ({hours_since:.1f}) >= "
                    f"threshold ({policy.max_hours_between_levels})"
                )

        return LevelingStatus(
            printer_name=printer_name,
            last_leveled_at=last_leveled_at,
            prints_since_level=prints_since,
            needs_leveling=needs,
            trigger_reason=reason,
            policy=policy,
            mesh_point_count=mesh_points,
            mesh_variance=round(mesh_var, 6) if mesh_var is not None else None,
        )

    # -- trigger leveling -----------------------------------------------

    def trigger_level(
        self,
        printer_name: str,
        adapter: Any,
        triggered_by: str = "manual",
    ) -> Dict[str, Any]:
        """Send a bed leveling command to the printer.

        Args:
            printer_name: Target printer name.
            adapter: The :class:`~kiln.printers.base.PrinterAdapter` instance.
            triggered_by: Source of the trigger (manual, auto_prints, etc.).

        Returns:
            Dict with ``success``, ``message``, and timing info.
        """
        policy = self.get_policy(printer_name)
        command = policy.gcode_command

        started_at = time.time()

        if self._bus is not None:
            from kiln.events import EventType
            self._bus.publish(
                EventType.LEVELING_TRIGGERED,
                data={
                    "printer_name": printer_name,
                    "command": command,
                    "triggered_by": triggered_by,
                },
                source=f"printer:{printer_name}",
            )

        try:
            result = adapter.send_gcode([command])
            completed_at = time.time()

            # Try to get mesh data after leveling
            mesh_data = None
            if hasattr(adapter, "get_bed_mesh"):
                mesh_data = adapter.get_bed_mesh()

            # Record in DB
            if self._db is not None:
                self._db.save_leveling({
                    "printer_name": printer_name,
                    "triggered_by": triggered_by,
                    "started_at": started_at,
                    "completed_at": completed_at,
                    "success": True,
                    "mesh_data": mesh_data,
                    "trigger_reason": triggered_by,
                })

            # Reset counter
            with self._lock:
                self._prints_since[printer_name] = 0

            if self._bus is not None:
                from kiln.events import EventType
                self._bus.publish(
                    EventType.LEVELING_COMPLETED,
                    data={
                        "printer_name": printer_name,
                        "duration_seconds": round(completed_at - started_at, 2),
                    },
                    source=f"printer:{printer_name}",
                )

            return {
                "success": True,
                "message": f"Bed leveling completed ({command})",
                "duration_seconds": round(completed_at - started_at, 2),
                "mesh_data": mesh_data,
            }

        except Exception as exc:
            logger.error("Bed leveling failed on %s: %s", printer_name, exc)
            if self._db is not None:
                self._db.save_leveling({
                    "printer_name": printer_name,
                    "triggered_by": triggered_by,
                    "started_at": started_at,
                    "completed_at": time.time(),
                    "success": False,
                    "mesh_data": None,
                    "trigger_reason": str(exc),
                })

            if self._bus is not None:
                from kiln.events import EventType
                self._bus.publish(
                    EventType.LEVELING_FAILED,
                    data={
                        "printer_name": printer_name,
                        "error": str(exc),
                    },
                    source=f"printer:{printer_name}",
                )

            return {
                "success": False,
                "message": f"Bed leveling failed: {exc}",
            }
