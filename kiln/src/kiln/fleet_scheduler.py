"""Fleet scheduler — capability matching, time estimation, and load balancing.

Extends the core job scheduler with smart printer selection:

- **Capability matching**: filter printers by material, build volume, nozzle size.
- **Print time estimation**: rough heuristic based on file size and material.
- **Load balancing**: score and rank printers by success rate, current load,
  and estimated queue wait time.

This module is stateless — it queries the registry and queue on each call.
The :class:`JobScheduler` in ``scheduler.py`` can delegate printer selection
here instead of using simple idle-printer ordering.

Example::

    caps = get_fleet_capabilities(registry, queue)
    best = select_best_printer(
        capabilities=caps,
        material="PLA",
        min_build_volume=(200, 200, 200),
    )
"""

from __future__ import annotations

import enum
import logging
import math
from dataclasses import asdict, dataclass
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class FleetSchedulingStrategy(enum.Enum):
    """Strategy used to select a printer for a queued job."""

    ROUND_ROBIN = "round_robin"
    LEAST_LOADED = "least_loaded"
    CAPABILITY_MATCHED = "capability_matched"


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class PrinterCapabilities:
    """Snapshot of a printer's capabilities and current workload.

    Built from registry data, queue depth, and (optionally) historical
    print-outcome records.
    """

    printer_id: str
    materials: list[str]
    max_build_volume: tuple[float, float, float]
    nozzle_sizes: list[float]
    is_available: bool
    current_load: float  # 0.0–1.0, estimated from queue depth
    estimated_queue_wait_minutes: int
    success_rate: float  # 0.0–1.0, historical

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        data = asdict(self)
        # asdict converts tuple to list; force back for clarity in JSON
        data["max_build_volume"] = list(self.max_build_volume)
        return data


@dataclass
class JobRequirements:
    """What a job needs from a printer.

    All fields are optional — omitted fields are not filtered on.
    """

    material: str | None = None
    min_build_volume: tuple[float, float, float] | None = None
    nozzle_size: float | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        data = asdict(self)
        if self.min_build_volume is not None:
            data["min_build_volume"] = list(self.min_build_volume)
        return data


@dataclass
class PrinterScore:
    """A scored printer candidate with breakdown."""

    printer_id: str
    total_score: float
    success_component: float
    load_component: float
    wait_component: float

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        return asdict(self)


# ---------------------------------------------------------------------------
# Material speed multipliers for time estimation
# ---------------------------------------------------------------------------

# Base rate: minutes per MB of G-code at 0.2 mm layer height.
_BASE_RATE_MIN_PER_MB: float = 45.0

# Multipliers relative to PLA. Slower materials get higher multipliers.
_MATERIAL_MULTIPLIERS: dict[str, float] = {
    "PLA": 1.0,
    "PETG": 1.15,
    "ABS": 1.1,
    "ASA": 1.1,
    "TPU": 1.6,
    "NYLON": 1.25,
    "PC": 1.2,
    "PVA": 1.3,
    "HIPS": 1.1,
    "WOOD": 1.2,
    "CARBON": 1.15,
}

# Layer-height adjustment: thinner layers ⇒ proportionally longer prints.
_REFERENCE_LAYER_HEIGHT_MM: float = 0.2


# ---------------------------------------------------------------------------
# Scoring weights
# ---------------------------------------------------------------------------

_WEIGHT_SUCCESS_RATE: float = 0.4
_WEIGHT_LOAD: float = 0.3
_WEIGHT_WAIT: float = 0.3


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def estimate_print_time(
    file_size_bytes: int,
    *,
    layer_height_mm: float = 0.2,
    material: str = "PLA",
) -> int:
    """Estimate print time in minutes from G-code file size.

    This is a rough heuristic, **not** a slicer estimate.  It's useful for
    queue-depth forecasting and load-balancing decisions, not for displaying
    an ETA to the user.

    :param file_size_bytes: Size of the G-code file in bytes.
    :param layer_height_mm: Layer height used for slicing.
    :param material: Material name (case-insensitive).
    :return: Estimated print time in minutes (always >= 1).
    """
    if file_size_bytes <= 0:
        return 1

    file_size_mb = file_size_bytes / (1024 * 1024)

    # Material multiplier (default to PLA speed for unknown materials)
    mat_key = material.upper().strip()
    material_mult = _MATERIAL_MULTIPLIERS.get(mat_key, 1.0)

    # Layer-height multiplier: half the layer height ≈ double the time
    layer_height_mm = max(layer_height_mm, 0.01)  # clamp to avoid div-by-zero
    layer_mult = _REFERENCE_LAYER_HEIGHT_MM / layer_height_mm

    estimated = file_size_mb * _BASE_RATE_MIN_PER_MB * material_mult * layer_mult
    return max(1, round(estimated))


def filter_by_capabilities(
    capabilities: list[PrinterCapabilities],
    requirements: JobRequirements,
) -> list[PrinterCapabilities]:
    """Return only printers that satisfy *requirements*.

    A printer passes if:
    - It is available (``is_available`` is True).
    - It supports the requested material (if specified).
    - Its build volume is >= the minimum on every axis (if specified).
    - It has a matching nozzle size (if specified).
    """
    result: list[PrinterCapabilities] = []

    for cap in capabilities:
        if not cap.is_available:
            continue

        # Material check (case-insensitive)
        if requirements.material is not None:
            mat_upper = requirements.material.upper().strip()
            if mat_upper not in [m.upper().strip() for m in cap.materials]:
                continue

        # Build-volume check (each axis must meet minimum)
        if requirements.min_build_volume is not None:
            rx, ry, rz = requirements.min_build_volume
            cx, cy, cz = cap.max_build_volume
            if cx < rx or cy < ry or cz < rz:
                continue

        # Nozzle-size check (must have the exact size)
        if requirements.nozzle_size is not None and requirements.nozzle_size not in cap.nozzle_sizes:
            continue

        result.append(cap)

    return result


def _score_printer(cap: PrinterCapabilities) -> PrinterScore:
    """Compute a composite score for a single printer.

    Score breakdown:
        success_component  = success_rate * 0.4
        load_component     = (1 - current_load) * 0.3
        wait_component     = (1 / max(estimated_queue_wait_minutes, 1)) * 0.3

    Higher is better.
    """
    success_component = cap.success_rate * _WEIGHT_SUCCESS_RATE
    load_component = (1.0 - cap.current_load) * _WEIGHT_LOAD

    # Avoid division by zero; idle printers with 0-min wait get full marks.
    wait_minutes = max(cap.estimated_queue_wait_minutes, 1)
    wait_component = (1.0 / wait_minutes) * _WEIGHT_WAIT

    total = success_component + load_component + wait_component

    return PrinterScore(
        printer_id=cap.printer_id,
        total_score=round(total, 6),
        success_component=round(success_component, 6),
        load_component=round(load_component, 6),
        wait_component=round(wait_component, 6),
    )


def select_best_printer(
    capabilities: list[PrinterCapabilities],
    *,
    material: str | None = None,
    min_build_volume: tuple[float, float, float] | None = None,
    nozzle_size: float | None = None,
    strategy: FleetSchedulingStrategy = FleetSchedulingStrategy.CAPABILITY_MATCHED,
) -> list[PrinterScore]:
    """Select and rank printers for a job.

    :param capabilities: Full fleet capabilities snapshot.
    :param material: Required material (e.g. ``"PLA"``).
    :param min_build_volume: Minimum (x, y, z) build volume in mm.
    :param nozzle_size: Required nozzle diameter in mm.
    :param strategy: Scheduling strategy to apply.
    :return: Printers ranked best-first.  Empty list if nothing matches.
    """
    if strategy == FleetSchedulingStrategy.ROUND_ROBIN:
        # Round-robin: return all available printers unsorted (caller rotates).
        available = [c for c in capabilities if c.is_available]
        return [_score_printer(c) for c in available]

    if strategy == FleetSchedulingStrategy.LEAST_LOADED:
        # Least-loaded: sort only by current_load, ignore capability matching.
        available = [c for c in capabilities if c.is_available]
        available.sort(key=lambda c: c.current_load)
        return [_score_printer(c) for c in available]

    # Default: CAPABILITY_MATCHED — filter then score.
    requirements = JobRequirements(
        material=material,
        min_build_volume=min_build_volume,
        nozzle_size=nozzle_size,
    )
    matched = filter_by_capabilities(capabilities, requirements)

    scored = [_score_printer(c) for c in matched]
    scored.sort(key=lambda s: s.total_score, reverse=True)
    return scored


def get_fleet_capabilities(
    registry,
    queue,
    *,
    printer_metadata: dict[str, dict[str, Any]] | None = None,
    persistence: object | None = None,
) -> list[PrinterCapabilities]:
    """Build a capabilities snapshot from live registry and queue state.

    :param registry: :class:`~kiln.registry.PrinterRegistry` instance.
    :param queue: :class:`~kiln.queue.PrintQueue` instance.
    :param printer_metadata: Optional mapping of printer_name → metadata dict
        with keys ``materials``, ``max_build_volume``, ``nozzle_sizes``.
        When not provided, sensible FDM defaults are used.
    :param persistence: Optional persistence layer (for historical success rates).
    :return: One :class:`PrinterCapabilities` per registered printer.
    """
    from kiln.printers.base import PrinterStatus
    from kiln.queue import JobStatus

    printer_metadata = printer_metadata or {}
    fleet_status = registry.get_fleet_status()

    # Count queued jobs per printer (None-targeted jobs count against all)
    all_jobs = queue.list_jobs(status=JobStatus.QUEUED)
    targeted_counts: dict[str, int] = {}
    untargeted_count = 0
    for job in all_jobs:
        if job.printer_name:
            targeted_counts[job.printer_name] = targeted_counts.get(job.printer_name, 0) + 1
        else:
            untargeted_count += 1

    total_printers = max(len(fleet_status), 1)

    result: list[PrinterCapabilities] = []

    for entry in fleet_status:
        name = entry["name"]
        meta = printer_metadata.get(name, {})

        # Capability defaults for generic FDM printer
        materials = meta.get("materials", ["PLA", "PETG", "ABS", "TPU"])
        raw_volume = meta.get("max_build_volume", (220.0, 220.0, 250.0))
        max_build_volume = (float(raw_volume[0]), float(raw_volume[1]), float(raw_volume[2]))
        nozzle_sizes = meta.get("nozzle_sizes", [0.4])

        is_available = entry["state"] in (PrinterStatus.IDLE.value, "idle")

        # Queue depth for this printer: targeted + fair share of untargeted
        targeted = targeted_counts.get(name, 0)
        fair_share = math.ceil(untargeted_count / total_printers)
        queue_depth = targeted + fair_share

        # Load: 0.0 (empty) to 1.0 (saturated).  Cap at 10 jobs = 1.0.
        current_load = min(queue_depth / 10.0, 1.0)

        # Rough wait: assume 30 min average per queued job
        estimated_queue_wait_minutes = queue_depth * 30

        # Historical success rate from persistence, or a conservative default
        success_rate = 0.8  # default
        if persistence is not None:
            try:
                rankings = persistence.suggest_printer_for_outcome(
                    file_hash=None,
                    material_type=None,
                )
                for r in rankings or []:
                    if r.get("printer_name") == name:
                        success_rate = r.get("success_rate", 0.8)
                        break
            except Exception:
                pass

        result.append(
            PrinterCapabilities(
                printer_id=name,
                materials=materials,
                max_build_volume=max_build_volume,
                nozzle_sizes=nozzle_sizes,
                is_available=is_available,
                current_load=round(current_load, 4),
                estimated_queue_wait_minutes=estimated_queue_wait_minutes,
                success_rate=round(success_rate, 4),
            )
        )

    return result
