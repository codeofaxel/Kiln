"""Print cost estimation from G-code analysis.

Parses G-code to extract filament extrusion totals, then calculates
material weight, filament cost, electricity cost, and total cost based
on configurable material profiles and electricity rates.
"""

from __future__ import annotations

import contextlib
import math
import os
import re
from dataclasses import asdict, dataclass, field
from typing import Any

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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# Common material database
BUILTIN_MATERIALS: dict[str, MaterialProfile] = {
    "PLA": MaterialProfile(
        name="PLA",
        density_g_per_cm3=1.24,
        cost_per_kg_usd=25.0,
        tool_temp_default=210.0,
        bed_temp_default=60.0,
    ),
    "PETG": MaterialProfile(
        name="PETG",
        density_g_per_cm3=1.27,
        cost_per_kg_usd=30.0,
        tool_temp_default=240.0,
        bed_temp_default=80.0,
    ),
    "ABS": MaterialProfile(
        name="ABS",
        density_g_per_cm3=1.04,
        cost_per_kg_usd=22.0,
        tool_temp_default=245.0,
        bed_temp_default=100.0,
    ),
    "TPU": MaterialProfile(
        name="TPU",
        density_g_per_cm3=1.21,
        cost_per_kg_usd=35.0,
        tool_temp_default=230.0,
        bed_temp_default=50.0,
    ),
    "ASA": MaterialProfile(
        name="ASA",
        density_g_per_cm3=1.07,
        cost_per_kg_usd=28.0,
        tool_temp_default=250.0,
        bed_temp_default=100.0,
    ),
    "NYLON": MaterialProfile(
        name="NYLON",
        density_g_per_cm3=1.14,
        cost_per_kg_usd=40.0,
        tool_temp_default=260.0,
        bed_temp_default=70.0,
    ),
    "PC": MaterialProfile(
        name="PC",
        density_g_per_cm3=1.20,
        cost_per_kg_usd=45.0,
        tool_temp_default=270.0,
        bed_temp_default=110.0,
    ),
    "PLA+": MaterialProfile(
        name="PLA+",
        density_g_per_cm3=1.24,
        cost_per_kg_usd=28.0,
        tool_temp_default=215.0,
        bed_temp_default=60.0,
    ),
    "CF-PLA": MaterialProfile(
        name="CF-PLA",
        density_g_per_cm3=1.30,
        cost_per_kg_usd=45.0,
        tool_temp_default=220.0,
        bed_temp_default=60.0,
    ),
    "SILK-PLA": MaterialProfile(
        name="SILK-PLA",
        density_g_per_cm3=1.24,
        cost_per_kg_usd=30.0,
        tool_temp_default=215.0,
        bed_temp_default=60.0,
    ),
    "HIPS": MaterialProfile(
        name="HIPS",
        density_g_per_cm3=1.04,
        cost_per_kg_usd=22.0,
        tool_temp_default=240.0,
        bed_temp_default=100.0,
    ),
    "PVA": MaterialProfile(
        name="PVA",
        density_g_per_cm3=1.23,
        cost_per_kg_usd=60.0,
        tool_temp_default=200.0,
        bed_temp_default=45.0,
    ),
    "PP": MaterialProfile(
        name="PP",
        density_g_per_cm3=0.90,
        cost_per_kg_usd=35.0,
        tool_temp_default=240.0,
        bed_temp_default=85.0,
    ),
    "PEEK": MaterialProfile(
        name="PEEK",
        density_g_per_cm3=1.30,
        cost_per_kg_usd=300.0,
        tool_temp_default=400.0,
        bed_temp_default=120.0,
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
    estimated_time_seconds: int | None = None
    electricity_cost_usd: float = 0.0
    electricity_rate_kwh: float = 0.12
    printer_wattage: float = 200.0
    total_cost_usd: float = 0.0
    warnings: list[str] = field(default_factory=list)
    support_weight_grams: float = 0.0
    support_cost_usd: float = 0.0
    adhesion_weight_grams: float = 0.0
    adhesion_cost_usd: float = 0.0
    total_plastic_volume_mm3: float = 0.0
    infill_percent: float = 20.0
    cost_breakdown: dict[str, float] = field(default_factory=dict)
    cost_summary: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
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


def _extract_e_value(line: str) -> float | None:
    """Extract the E parameter value from a G-code line."""
    m = _E_PATTERN.search(line)
    if m:
        return float(m.group(1))
    return None


def _parse_time_from_comments(lines: list[str]) -> int | None:
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
        custom_materials: dict[str, MaterialProfile] | None = None,
    ) -> None:
        self._materials = dict(BUILTIN_MATERIALS)
        if custom_materials:
            self._materials.update(custom_materials)

    @property
    def materials(self) -> dict[str, MaterialProfile]:
        """Return available material profiles."""
        return dict(self._materials)

    def get_material(self, name: str) -> MaterialProfile | None:
        """Look up a material by name (case-insensitive)."""
        return self._materials.get(name.upper())

    def estimate_from_file(
        self,
        file_path: str,
        material: str = "PLA",
        electricity_rate: float = 0.12,
        printer_wattage: float = 200.0,
    ) -> CostEstimate:
        """Estimate cost from a G-code or 3MF file on disk.

        For ``.3mf`` files (including Bambu ``.gcode.3mf``), the slicer
        metadata inside the archive is used when available, which is more
        reliable than parsing the proprietary gcode within.
        """
        if not os.path.isfile(file_path):
            raise FileNotFoundError(f"G-code file not found: {file_path}")

        # Try 3MF metadata extraction first for .3mf files.
        if file_path.lower().endswith(".3mf"):
            result = self._estimate_from_3mf_metadata(
                file_path,
                material=material,
                electricity_rate=electricity_rate,
                printer_wattage=printer_wattage,
            )
            if result is not None:
                return result

        with open(file_path, errors="replace") as f:
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
        lines: list[str],
        file_name: str = "<unknown>",
        material: str = "PLA",
        electricity_rate: float = 0.12,
        printer_wattage: float = 200.0,
    ) -> CostEstimate:
        """Estimate cost from a list of G-code lines."""
        warnings: list[str] = []

        profile = self.get_material(material)
        if profile is None:
            warnings.append(f"Unknown material '{material}', using PLA defaults")
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

        # Round only at the final output — avoid rounding intermediate values
        # to prevent accumulation errors.  Round total_cost from unrounded
        # intermediates so the result is as accurate as possible.
        total_cost = filament_cost + electricity_cost

        return CostEstimate(
            file_name=file_name,
            material=profile.name,
            filament_length_meters=round(filament_length_m, 3),
            filament_weight_grams=round(weight_g, 2),
            filament_cost_usd=round(filament_cost, 4),
            estimated_time_seconds=est_time,
            electricity_cost_usd=round(electricity_cost, 4),
            electricity_rate_kwh=electricity_rate,
            printer_wattage=printer_wattage,
            total_cost_usd=round(total_cost, 2),
            warnings=warnings,
        )

    def estimate_from_mesh(
        self,
        file_path: str,
        material: str = "PLA",
        infill_percent: float = 20.0,
        wall_layers: int = 3,
        layer_height_mm: float = 0.2,
        nozzle_mm: float = 0.4,
        include_supports: bool = False,
        support_density: float = 15.0,
        adhesion_type: str = "none",
        electricity_rate: float = 0.12,
        printer_wattage: float = 200.0,
    ) -> CostEstimate:
        """Estimate print cost directly from a 3D mesh file (STL/OBJ/GLB).

        Analyzes mesh geometry to compute material volume, weight, filament
        length, support/adhesion costs, electricity, and total cost.  Uses
        only stdlib-based mesh parsing — no external dependencies required.

        :param file_path: Path to an STL, OBJ, or 3MF mesh file.
        :param material: Filament material name (case-insensitive).
        :param infill_percent: Interior infill density (0-100).
        :param wall_layers: Number of perimeter wall layers.
        :param layer_height_mm: Slicer layer height in mm.
        :param nozzle_mm: Nozzle diameter in mm.
        :param include_supports: Whether to estimate support material.
        :param support_density: Support infill density (0-100).
        :param adhesion_type: ``"none"``, ``"brim"``, or ``"raft"``.
        :param electricity_rate: Electricity cost per kWh in USD.
        :param printer_wattage: Printer power consumption in watts.
        :returns: :class:`CostEstimate` with full cost breakdown.
        :raises FileNotFoundError: If *file_path* does not exist.
        :raises ValueError: If the mesh has zero or negative volume.
        """
        from kiln.generation.validation import analyze_mesh

        if not os.path.isfile(file_path):
            raise FileNotFoundError(f"Mesh file not found: {file_path}")

        warnings: list[str] = []

        # Analyze mesh geometry (stdlib-based, no external deps)
        analysis = analyze_mesh(file_path)
        if analysis.printability_issues and analysis.volume_mm3 <= 0:
            raise ValueError(
                f"Mesh has zero or negative volume ({analysis.volume_mm3:.2f} mm³). "
                f"The file may be non-manifold or empty."
            )

        total_volume_mm3 = analysis.volume_mm3
        surface_area_mm2 = analysis.surface_area_mm2

        if total_volume_mm3 <= 0:
            raise ValueError(
                f"Mesh has zero or negative volume ({total_volume_mm3:.2f} mm³). "
                f"The file may be non-manifold or empty."
            )

        # Material lookup (case-insensitive)
        profile = self.get_material(material)
        if profile is None:
            warnings.append(f"Unknown material '{material}', using PLA defaults")
            profile = BUILTIN_MATERIALS["PLA"]

        # --- Shell and infill volume ---
        shell_thickness_mm = wall_layers * nozzle_mm
        shell_volume_mm3 = surface_area_mm2 * shell_thickness_mm
        interior_volume_mm3 = max(0.0, total_volume_mm3 - shell_volume_mm3)
        infill_volume_mm3 = interior_volume_mm3 * (infill_percent / 100.0)
        total_plastic_mm3 = shell_volume_mm3 + infill_volume_mm3

        # --- Weight and filament length ---
        density = profile.density_g_per_cm3
        weight_g = (total_plastic_mm3 / 1000.0) * density

        filament_radius_mm = profile.filament_diameter_mm / 2.0
        cross_section_mm2 = math.pi * filament_radius_mm * filament_radius_mm
        filament_length_mm = total_plastic_mm3 / cross_section_mm2
        filament_length_m = filament_length_mm / 1000.0

        filament_cost = (weight_g / 1000.0) * profile.cost_per_kg_usd

        # --- Support estimation ---
        support_weight_g = 0.0
        support_cost = 0.0
        if include_supports and analysis.overhang_percentage > 0:
            # Estimate support volume from overhang percentage and part height
            dims = analysis.dimensions_mm or {}
            part_height = dims.get("height_mm", 0.0)
            overhang_frac = analysis.overhang_percentage / 100.0
            # Approximate: overhang area * average height * support density
            overhang_area = surface_area_mm2 * overhang_frac
            avg_height = part_height / 2.0  # average height of overhangs
            support_volume_mm3 = (
                overhang_area * avg_height * (support_density / 100.0)
            )
            support_weight_g = (support_volume_mm3 / 1000.0) * density
            support_cost = (support_weight_g / 1000.0) * profile.cost_per_kg_usd

        # --- Adhesion estimation ---
        adhesion_weight_g = 0.0
        adhesion_cost = 0.0
        dims = analysis.dimensions_mm or {}
        bbox_x = dims.get("width_mm", 0.0)
        bbox_y = dims.get("depth_mm", 0.0)

        if adhesion_type == "brim":
            # Approximate footprint perimeter from bounding box
            perimeter_mm = 2.0 * (bbox_x + bbox_y)
            brim_width_mm = 8.0
            brim_area_mm2 = perimeter_mm * brim_width_mm
            brim_volume_mm3 = brim_area_mm2 * layer_height_mm
            adhesion_weight_g = (brim_volume_mm3 / 1000.0) * density
            adhesion_cost = (adhesion_weight_g / 1000.0) * profile.cost_per_kg_usd
        elif adhesion_type == "raft":
            margin_mm = 3.0
            raft_layers = 3
            raft_volume_mm3 = (
                (bbox_x + 2.0 * margin_mm)
                * (bbox_y + 2.0 * margin_mm)
                * (raft_layers * layer_height_mm)
            )
            adhesion_weight_g = (raft_volume_mm3 / 1000.0) * density
            adhesion_cost = (adhesion_weight_g / 1000.0) * profile.cost_per_kg_usd

        # --- Print time estimation ---
        total_extrude_volume = total_plastic_mm3 + (
            support_weight_g / density * 1000.0 if support_weight_g > 0 else 0.0
        )
        print_speed_mm_s = 60.0
        extrusion_cross_section = nozzle_mm * layer_height_mm
        travel_overhead = 1.3
        if extrusion_cross_section > 0:
            linear_distance_mm = total_extrude_volume / extrusion_cross_section
            est_time_s = int((linear_distance_mm / print_speed_mm_s) * travel_overhead)
        else:
            est_time_s = 0

        # --- Electricity cost ---
        electricity_cost = 0.0
        if est_time_s > 0:
            hours = est_time_s / 3600.0
            kwh = (printer_wattage / 1000.0) * hours
            electricity_cost = kwh * electricity_rate

        # --- Totals ---
        total_cost = filament_cost + support_cost + adhesion_cost + electricity_cost

        cost_breakdown = {
            "filament": round(filament_cost, 4),
            "support": round(support_cost, 4),
            "adhesion": round(adhesion_cost, 4),
            "electricity": round(electricity_cost, 4),
        }

        cost_summary = {
            "material": round(filament_cost + support_cost + adhesion_cost, 2),
            "electricity": round(electricity_cost, 2),
        }

        return CostEstimate(
            file_name=os.path.basename(file_path),
            material=profile.name,
            filament_length_meters=round(filament_length_m, 3),
            filament_weight_grams=round(weight_g, 2),
            filament_cost_usd=round(filament_cost, 4),
            estimated_time_seconds=est_time_s if est_time_s > 0 else None,
            electricity_cost_usd=round(electricity_cost, 4),
            electricity_rate_kwh=electricity_rate,
            printer_wattage=printer_wattage,
            total_cost_usd=round(total_cost, 2),
            warnings=warnings,
            support_weight_grams=round(support_weight_g, 2),
            support_cost_usd=round(support_cost, 4),
            adhesion_weight_grams=round(adhesion_weight_g, 2),
            adhesion_cost_usd=round(adhesion_cost, 4),
            total_plastic_volume_mm3=round(total_plastic_mm3, 2),
            infill_percent=infill_percent,
            cost_breakdown=cost_breakdown,
            cost_summary=cost_summary,
        )

    def _estimate_from_3mf_metadata(
        self,
        file_path: str,
        material: str = "PLA",
        electricity_rate: float = 0.12,
        printer_wattage: float = 200.0,
    ) -> CostEstimate | None:
        """Extract cost data from 3MF slicer metadata (slice_info.config).

        Returns ``None`` if the archive doesn't contain usable metadata,
        allowing the caller to fall back to gcode line parsing.

        Bambu Studio, OrcaSlicer, and compatible slicers embed a
        ``Metadata/slice_info.config`` XML file with per-plate estimates
        including filament weight (g), length (m), print time (s), and
        material type.
        """
        import xml.etree.ElementTree as ET
        import zipfile

        try:
            with zipfile.ZipFile(file_path, "r") as zf:
                if "Metadata/slice_info.config" not in zf.namelist():
                    return None
                xml_data = zf.read("Metadata/slice_info.config").decode("utf-8")
        except (zipfile.BadZipFile, OSError, KeyError):
            return None

        try:
            root = ET.fromstring(xml_data)
        except ET.ParseError:
            return None

        plate = root.find("plate")
        if plate is None:
            return None

        # Extract plate-level metadata.
        meta: dict[str, str] = {}
        for md in plate.findall("metadata"):
            key = md.get("key", "")
            val = md.get("value", "")
            if key and val:
                meta[key] = val

        # Aggregate filament usage across all filament entries on this plate.
        total_weight_g = 0.0
        total_length_m = 0.0
        detected_material: str | None = None
        for fil in plate.findall("filament"):
            used_g = fil.get("used_g", "0")
            used_m = fil.get("used_m", "0")
            try:
                total_weight_g += float(used_g)
                total_length_m += float(used_m)
            except ValueError:
                continue
            if detected_material is None:
                detected_material = fil.get("type")

        # If we got no usable weight/length data, fall back.
        if total_weight_g <= 0 and total_length_m <= 0:
            return None

        # Use detected material from the 3MF, fall back to caller's choice.
        mat_name = (detected_material or material).upper()
        profile = self.get_material(mat_name)
        warnings: list[str] = []
        if profile is None:
            warnings.append(f"Unknown material '{mat_name}', using PLA defaults")
            profile = BUILTIN_MATERIALS["PLA"]

        # Filament cost from weight.
        filament_cost = (total_weight_g / 1000.0) * profile.cost_per_kg_usd

        # Print time from prediction metadata.
        est_time: int | None = None
        prediction = meta.get("prediction")
        if prediction:
            with contextlib.suppress(ValueError):
                est_time = int(prediction)

        # Electricity cost.
        electricity_cost = 0.0
        if est_time and est_time > 0:
            hours = est_time / 3600.0
            kwh = (printer_wattage / 1000.0) * hours
            electricity_cost = kwh * electricity_rate

        total_cost = filament_cost + electricity_cost

        return CostEstimate(
            file_name=os.path.basename(file_path),
            material=profile.name,
            filament_length_meters=round(total_length_m, 3),
            filament_weight_grams=round(total_weight_g, 2),
            filament_cost_usd=round(filament_cost, 4),
            estimated_time_seconds=est_time,
            electricity_cost_usd=round(electricity_cost, 4),
            electricity_rate_kwh=electricity_rate,
            printer_wattage=printer_wattage,
            total_cost_usd=round(total_cost, 2),
            warnings=warnings,
        )

    def _parse_extrusion(self, lines: list[str]) -> float:
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
            if not (
                upper.startswith("G0 ")
                or upper.startswith("G1 ")
                or upper.startswith("G0\t")
                or upper.startswith("G1\t")
            ):
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
