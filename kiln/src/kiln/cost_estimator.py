"""Print cost estimation from G-code analysis.

Parses G-code to extract filament extrusion totals, then calculates
material weight, filament cost, electricity cost, and total cost based
on configurable material profiles and electricity rates.
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Material profiles
# ---------------------------------------------------------------------------

@dataclass
class MaterialProfile:
    """Physical and cost properties of a filament material."""

    name: str
    density_g_per_cm3: float
    cost_per_kg_usd: float
    filament_diameter_mm: float = 1.75
    tool_temp_default: float = 200.0
    bed_temp_default: float = 60.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# Common material database
BUILTIN_MATERIALS: Dict[str, MaterialProfile] = {
    "PLA": MaterialProfile(
        name="PLA", density_g_per_cm3=1.24, cost_per_kg_usd=25.0,
        tool_temp_default=210.0, bed_temp_default=60.0,
    ),
    "PETG": MaterialProfile(
        name="PETG", density_g_per_cm3=1.27, cost_per_kg_usd=30.0,
        tool_temp_default=240.0, bed_temp_default=80.0,
    ),
    "ABS": MaterialProfile(
        name="ABS", density_g_per_cm3=1.04, cost_per_kg_usd=22.0,
        tool_temp_default=245.0, bed_temp_default=100.0,
    ),
    "TPU": MaterialProfile(
        name="TPU", density_g_per_cm3=1.21, cost_per_kg_usd=35.0,
        tool_temp_default=230.0, bed_temp_default=50.0,
    ),
    "ASA": MaterialProfile(
        name="ASA", density_g_per_cm3=1.07, cost_per_kg_usd=28.0,
        tool_temp_default=250.0, bed_temp_default=100.0,
    ),
    "NYLON": MaterialProfile(
        name="NYLON", density_g_per_cm3=1.14, cost_per_kg_usd=40.0,
        tool_temp_default=260.0, bed_temp_default=70.0,
    ),
    "PC": MaterialProfile(
        name="PC", density_g_per_cm3=1.20, cost_per_kg_usd=45.0,
        tool_temp_default=270.0, bed_temp_default=110.0,
    ),
}


# ---------------------------------------------------------------------------
# Cost estimate result
# ---------------------------------------------------------------------------

@dataclass
class CostEstimate:
    """Result of a print cost estimation."""

    file_name: str
    material: str
    filament_length_meters: float
    filament_weight_grams: float
    filament_cost_usd: float
    estimated_time_seconds: Optional[int] = None
    electricity_cost_usd: float = 0.0
    electricity_rate_kwh: float = 0.12
    printer_wattage: float = 200.0
    total_cost_usd: float = 0.0
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# G-code parsing helpers
# ---------------------------------------------------------------------------

_E_PATTERN = re.compile(r"E([-+]?\d+\.?\d*)", re.IGNORECASE)
_TIME_PATTERNS = [
    # PrusaSlicer: ; estimated printing time (normal mode) = 1h 23m 45s
    re.compile(
        r";\s*estimated printing time.*?=\s*"
        r"(?:(\d+)h\s*)?(?:(\d+)m\s*)?(?:(\d+)s)?",
        re.IGNORECASE,
    ),
    # Cura: ;TIME:5025
    re.compile(r";\s*TIME:\s*(\d+)", re.IGNORECASE),
    # OrcaSlicer: ; total estimated time: 1h 23m 45s
    re.compile(
        r";\s*total estimated time.*?:\s*"
        r"(?:(\d+)h\s*)?(?:(\d+)m\s*)?(?:(\d+)s)?",
        re.IGNORECASE,
    ),
]


def _extract_e_value(line: str) -> Optional[float]:
    """Extract the E parameter value from a G-code line."""
    m = _E_PATTERN.search(line)
    if m:
        return float(m.group(1))
    return None


def _parse_time_from_comments(lines: List[str]) -> Optional[int]:
    """Try to extract estimated print time from slicer comments."""
    for line in lines:
        if not line.startswith(";"):
            continue

        # Try Cura-style TIME:seconds first (simplest)
        for pattern in _TIME_PATTERNS:
            m = pattern.search(line)
            if m:
                groups = m.groups()
                # Cura pattern has 1 group (seconds total)
                if len(groups) == 1 and groups[0] is not None:
                    return int(groups[0])
                # H/M/S patterns have 3 groups
                if len(groups) == 3:
                    h = int(groups[0]) if groups[0] else 0
                    mins = int(groups[1]) if groups[1] else 0
                    s = int(groups[2]) if groups[2] else 0
                    total = h * 3600 + mins * 60 + s
                    if total > 0:
                        return total
    return None


# ---------------------------------------------------------------------------
# Core estimator
# ---------------------------------------------------------------------------

class CostEstimator:
    """Estimates print cost from G-code files."""

    def __init__(
        self,
        custom_materials: Optional[Dict[str, MaterialProfile]] = None,
    ) -> None:
        self._materials = dict(BUILTIN_MATERIALS)
        if custom_materials:
            self._materials.update(custom_materials)

    @property
    def materials(self) -> Dict[str, MaterialProfile]:
        """Return available material profiles."""
        return dict(self._materials)

    def get_material(self, name: str) -> Optional[MaterialProfile]:
        """Look up a material by name (case-insensitive)."""
        return self._materials.get(name.upper())

    def estimate_from_file(
        self,
        file_path: str,
        material: str = "PLA",
        electricity_rate: float = 0.12,
        printer_wattage: float = 200.0,
    ) -> CostEstimate:
        """Estimate cost from a G-code file on disk."""
        if not os.path.isfile(file_path):
            raise FileNotFoundError(f"G-code file not found: {file_path}")

        with open(file_path, "r", errors="replace") as f:
            lines = f.readlines()

        return self.estimate_from_gcode(
            lines=lines,
            file_name=os.path.basename(file_path),
            material=material,
            electricity_rate=electricity_rate,
            printer_wattage=printer_wattage,
        )

    def estimate_from_gcode(
        self,
        lines: List[str],
        file_name: str = "<unknown>",
        material: str = "PLA",
        electricity_rate: float = 0.12,
        printer_wattage: float = 200.0,
    ) -> CostEstimate:
        """Estimate cost from a list of G-code lines."""
        warnings: List[str] = []

        profile = self.get_material(material)
        if profile is None:
            warnings.append(
                f"Unknown material '{material}', using PLA defaults"
            )
            profile = BUILTIN_MATERIALS["PLA"]

        # Parse extrusion and time
        total_e_mm = self._parse_extrusion(lines)
        est_time = _parse_time_from_comments(lines)

        if total_e_mm <= 0:
            warnings.append("No extrusion commands found in G-code")

        # Convert E-axis mm to filament length in meters
        filament_length_m = total_e_mm / 1000.0

        # Cross-section area of filament (mm^2)
        radius_mm = profile.filament_diameter_mm / 2.0
        cross_section_mm2 = math.pi * radius_mm * radius_mm

        # Volume in cm^3 (mm * mm^2 = mm^3, /1000 = cm^3)
        volume_cm3 = (total_e_mm * cross_section_mm2) / 1000.0

        # Weight
        weight_g = volume_cm3 * profile.density_g_per_cm3

        # Filament cost
        filament_cost = (weight_g / 1000.0) * profile.cost_per_kg_usd

        # Electricity cost
        electricity_cost = 0.0
        if est_time and est_time > 0:
            hours = est_time / 3600.0
            kwh = (printer_wattage / 1000.0) * hours
            electricity_cost = kwh * electricity_rate

        total_cost = filament_cost + electricity_cost

        return CostEstimate(
            file_name=file_name,
            material=profile.name,
            filament_length_meters=round(filament_length_m, 3),
            filament_weight_grams=round(weight_g, 2),
            filament_cost_usd=round(filament_cost, 2),
            estimated_time_seconds=est_time,
            electricity_cost_usd=round(electricity_cost, 2),
            electricity_rate_kwh=electricity_rate,
            printer_wattage=printer_wattage,
            total_cost_usd=round(total_cost, 2),
            warnings=warnings,
        )

    def _parse_extrusion(self, lines: List[str]) -> float:
        """Parse total filament extrusion in mm from G-code lines.

        Handles both absolute (default) and relative (M83) E-axis modes.
        Filters out retractions (negative E in absolute mode detected by
        comparing to previous E value).
        """
        total_e_mm = 0.0
        last_e = 0.0
        relative_mode = False

        for raw_line in lines:
            line = raw_line.strip()

            # Skip empty lines and comments
            if not line or line.startswith(";"):
                continue

            # Strip inline comments
            if ";" in line:
                line = line[: line.index(";")].strip()

            upper = line.upper()

            # Track E-axis mode
            if upper.startswith("M82"):
                relative_mode = False
                last_e = 0.0
                continue
            if upper.startswith("M83"):
                relative_mode = True
                continue
            # G92 E0 resets the E position
            if upper.startswith("G92"):
                e_val = _extract_e_value(line)
                if e_val is not None:
                    last_e = e_val
                continue

            # Only process G0/G1 moves
            if not (upper.startswith("G0 ") or upper.startswith("G1 ")
                    or upper.startswith("G0\t") or upper.startswith("G1\t")):
                continue

            e_val = _extract_e_value(line)
            if e_val is None:
                continue

            if relative_mode:
                # In relative mode, positive E = extrusion, negative = retraction
                if e_val > 0:
                    total_e_mm += e_val
            else:
                # Absolute mode: extrusion = current - last (when positive)
                delta = e_val - last_e
                if delta > 0:
                    total_e_mm += delta
                last_e = e_val

        return total_e_mm
