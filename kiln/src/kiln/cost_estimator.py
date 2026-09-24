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
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from kiln.gcode import (
    extruded_mm_per_tool,
    slicer_filament_totals,
    slicer_filament_types,
    slicer_print_time,
)

#: How far Kiln's own count may sit from the slicer's ``filament used``
#: total before the estimate says so.  The count matched OrcaSlicer to
#: the centimetre on a 320,000-line plate; a gap wider than this means a
#: G-code dialect the counter does not read, and a confident number would
#: be the wrong answer.
_HEADER_DISAGREEMENT_FRACTION = 0.05

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

#: The row a material nobody named, and no file names, is weighed and
#: priced as.  Every estimate that falls back to it says so.
DEFAULT_MATERIAL = "PLA"


def resolve_material(
    name: str | None,
    *,
    table: dict[str, MaterialProfile] | None = None,
) -> MaterialProfile | None:
    """The row of the material table a material name lands on, or ``None``.

    The one lookup every estimate uses, so a name weighs and prices the
    same at every door.  Spellings arrive from three vocabularies -- a
    person's (``"petg"``, ``"PLA Basic"``), a printer's AMS (``"PLA-CF"``,
    ``"PA-CF"``) and the table's own (``"CF-PLA"``, ``"NYLON"``) -- so the
    lookup tries, in order: the row itself (spaces and underscores read as
    hyphens, a trailing ``PLUS`` as ``+``), the alias table
    :mod:`kiln.materials` keeps (``PA`` is nylon), the hyphen pair reversed
    (an AMS writes the modifier last, the table first), and finally the
    family the name starts with (``PETG-HF`` is PETG for weighing and
    pricing).  ``None`` means the table has no row and no family for it;
    the caller decides what that means and says so.

    :param table: The rows to search, :data:`BUILTIN_MATERIALS` when
        omitted; a :class:`CostEstimator` passes its own, custom rows
        included.
    """
    if not name:
        return None
    rows = BUILTIN_MATERIALS if table is None else table
    key = re.sub(r"[\s_]+", "-", str(name).strip().upper())
    key = re.sub(r"-?PLUS$", "+", key)
    if not key:
        return None

    def _alias(candidate: str) -> str | None:
        try:
            from kiln.materials import normalise_material_type

            return normalise_material_type(candidate)
        except Exception:  # noqa: BLE001 -- an alias table that cannot load is no alias
            return None

    def _row(candidate: str | None) -> MaterialProfile | None:
        return rows.get(candidate) if candidate else None

    hit = _row(key) or _row(_alias(key))
    if hit is None and "-" in key:
        head, _, tail = key.partition("-")
        hit = _row(f"{tail}-{head}")
    if hit is None:
        family = re.match(r"[A-Z]+", key)
        if family:
            hit = _row(family.group(0)) or _row(_alias(family.group(0)))
    return hit


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
    #: Where the filament figures came from: ``slicer_header`` (the
    #: slicer's own ``filament used`` totals, the primary source when the
    #: file carries them), ``gcode_moves`` (Kiln's count of the extruding
    #: moves — the only source for a file with no slicer totals), ``3mf``
    #: (the archive's slice metadata) or ``mesh`` (estimated from geometry).
    filament_source: str = "gcode_moves"
    #: A print file's filaments, one entry per filament it uses, in tool
    #: order: ``tool`` (``0`` is ``T0``), the ``material`` it is priced and
    #: weighed as, what the file names it (``file_material``, ``None`` when
    #: it names nothing), where the material came from, and its length,
    #: weight and filament cost.  Empty for a mesh estimate.
    filaments: list[dict[str, Any]] = field(default_factory=list)
    #: Where a print file's ``material`` came from: ``named`` (the caller
    #: named it), ``file`` (the type the file was sliced for) or
    #: ``default`` (at least one filament had neither, so it is priced as
    #: PLA and a warning says which).  ``None`` for a mesh estimate.
    material_source: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# G-code parsing helpers
# ---------------------------------------------------------------------------

def _parse_time_from_comments(lines: list[str]) -> int | None:
    """The slicer's own print time in seconds, read by the one reader of it
    (:func:`kiln.gcode.slicer_print_time`), or ``None``."""
    printed = slicer_print_time(_comment_text(lines))
    return printed.seconds if printed is not None else None


def _sliced_3mf_gcode_lines(file_path: str) -> list[str]:
    """The lines of the G-code a sliced 3MF prints
    (:func:`kiln.gcode_metadata.sliced_gcode_member`).

    :raises ValueError: when the archive holds no G-code (a model, not a
        print file), is not a readable archive, or unpacks to more than a
        G-code file Kiln scans (:data:`kiln.gcode._MAX_SCAN_BYTES`), which
        is refused before anything is unpacked.
    """
    import zipfile

    from kiln.gcode import _MAX_SCAN_BYTES
    from kiln.gcode_metadata import sliced_gcode_member

    name = os.path.basename(file_path)
    try:
        with zipfile.ZipFile(file_path) as zf:
            member = sliced_gcode_member(zf)
            if member is None:
                raise ValueError(
                    f"{name} holds a model, not sliced G-code, so it has no print cost to read yet; "
                    "slice it first (slice_and_estimate slices and estimates in one step)"
                )
            info = zf.getinfo(member)
            if info.file_size > _MAX_SCAN_BYTES:
                raise ValueError(
                    f"{name}'s G-code unpacks to {info.file_size / 1024 / 1024:.0f} MB, "
                    f"more than the {_MAX_SCAN_BYTES // (1024 * 1024)} MB Kiln reads"
                )
            text = zf.read(member).decode("utf-8", errors="replace")
    except zipfile.BadZipFile as exc:
        raise ValueError(f"{name} is not a readable 3MF archive: {exc}") from exc
    return text.splitlines(keepends=True)


def _comment_text(lines: list[str]) -> str:
    """Only the comment lines, joined: the slicer's own figures are all on
    them, and a plate's moves are 99% of its 8 MB."""
    return "\n".join(line.rstrip("\r\n") for line in lines if line.lstrip().startswith(";"))


@dataclass
class FilamentPricing:
    """What a print file's filaments weigh and cost, one entry per filament.

    ``filaments`` holds the per-filament entries :class:`CostEstimate`
    reports; ``material`` names what they are priced as (``"PLA + PETG"``
    for a plate of two); ``material_source`` and ``filament_source`` read
    as they do on :class:`CostEstimate`.
    """

    filaments: list[dict[str, Any]]
    material: str
    material_source: str
    weight_g: float
    cost_usd: float
    length_mm: float
    filament_source: str
    warnings: list[str] = field(default_factory=list)

    @property
    def cost_per_kg_usd(self) -> float | None:
        """The plate's price per kg: its row's price for one material, the
        weighted mix for several, ``None`` when nothing is weighed."""
        return self.cost_usd / (self.weight_g / 1000.0) if self.weight_g > 0 else None


def _grams_from_length(length_mm: float, profile: MaterialProfile) -> float:
    radius_mm = profile.filament_diameter_mm / 2.0
    return length_mm * math.pi * radius_mm * radius_mm * profile.density_g_per_cm3 / 1000.0


def _filament_numbers(tools: list[int]) -> str:
    """``filament 2`` / ``filaments 1 and 3``: tools as a person counts them."""
    numbers = [str(tool + 1) for tool in tools]
    if len(numbers) == 1:
        return f"filament {numbers[0]}"
    return f"filaments {', '.join(numbers[:-1])} and {numbers[-1]}"


def _price_filaments(
    lookup: Callable[[str | None], MaterialProfile | None],
    *,
    lengths_mm: Sequence[float],
    grams: Sequence[float],
    file_types: tuple[str, ...],
    named: str | None,
    slicer_weighed: bool,
    filament_source: str,
) -> FilamentPricing:
    """Weigh and price each filament a print file uses, by tool.

    *lengths_mm* and *grams* are indexed by tool, *file_types* by filament
    slot (the same index).  A filament is priced as the material *named*
    when the caller named one, else as the type the file was sliced for,
    else as PLA -- and a warning says which filaments were guessed.

    Weight is converted only between materials: grams weighed as one
    material (the slicer's own figure, or Kiln's weighing of the length at
    the file's type) are scaled by the density ratio when the filament is
    priced as another.  A filament with a length and no grams is weighed
    from its length at the priced material's density.
    """
    warnings: list[str] = []
    default = lookup(DEFAULT_MATERIAL) or BUILTIN_MATERIALS[DEFAULT_MATERIAL]
    named_row: MaterialProfile | None = None
    if named:
        named_row = lookup(named)
        if named_row is None:
            warnings.append(f"Unknown material '{named}', using PLA defaults")
            named_row = default

    entries: list[dict[str, Any]] = []
    sliced_as_other: list[str] = []
    untyped: list[int] = []
    unknown: list[str] = []
    weight_total = 0.0
    cost_total = 0.0
    for tool, length in enumerate(lengths_mm):
        g = float(grams[tool]) if tool < len(grams) else 0.0
        if length <= 0 and g <= 0:
            continue
        word = file_types[tool] if tool < len(file_types) and file_types[tool] else None
        file_row = lookup(word)
        if named_row is not None:
            row, source = named_row, "named"
            if file_row is not None and file_row.name != row.name and word not in sliced_as_other:
                sliced_as_other.append(word)
        elif file_row is not None:
            row, source = file_row, "file"
        else:
            row, source = default, "default"
            if word is None:
                untyped.append(tool)
            elif word not in unknown:
                unknown.append(word)

        if g <= 0:
            g = _grams_from_length(length, row)
        else:
            # What the grams were weighed as: the file's own type (the
            # slicer weighs what it sliced, and so does Kiln's weigher,
            # PLA when the file names nothing it knows).  A slicer's
            # grams for a type Kiln cannot read stay as the slicer wrote.
            weighed_as = resolve_material(word) or (None if slicer_weighed else default)
            if weighed_as is not None and weighed_as.name != row.name:
                g *= row.density_g_per_cm3 / weighed_as.density_g_per_cm3
        cost = g / 1000.0 * row.cost_per_kg_usd
        weight_total += g
        cost_total += cost
        entries.append(
            {
                "tool": tool,
                "material": row.name,
                "file_material": word,
                "material_source": source,
                "length_m": round(length / 1000.0, 3),
                "weight_g": round(g, 2),
                "cost_usd": round(cost, 4),
            }
        )

    if sliced_as_other and named_row is not None:
        warnings.append(
            f"The file was sliced for {', '.join(sliced_as_other)}, but {named_row.name} "
            f"was named, so it is priced and weighed as {named_row.name}"
        )
    if untyped:
        if len(untyped) == len(entries):
            what = "what filament it was sliced for, so it is"
        elif len(untyped) == 1:
            what = f"what {_filament_numbers(untyped)} was sliced for, so it is"
        else:
            what = f"what {_filament_numbers(untyped)} were sliced for, so they are"
        warnings.append(
            f"The file does not say {what} priced as {default.name}; "
            "name the material to price it as another"
        )
    for word in unknown:
        warnings.append(
            f"The file was sliced for {word}, which Kiln's material table does not have, "
            f"so that filament is priced as {default.name}; name the material to price it as another"
        )

    if entries:
        names = list(dict.fromkeys(entry["material"] for entry in entries))
        sources = {entry["material_source"] for entry in entries}
        source = "named" if named_row is not None else ("default" if "default" in sources else "file")
        material = " + ".join(names)
    else:
        # Nothing extruded: the material is still what it would have been.
        first = next((t for t in file_types if t), None)
        first_row = lookup(first)
        if named_row is not None:
            material, source = named_row.name, "named"
        elif first_row is not None:
            material, source = first_row.name, "file"
        else:
            material, source = default.name, "default"
    return FilamentPricing(
        filaments=entries,
        material=material,
        material_source=source,
        weight_g=weight_total,
        cost_usd=cost_total,
        length_mm=float(sum(lengths_mm)),
        filament_source=filament_source,
        warnings=warnings,
    )


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

    def get_material(self, name: str | None) -> MaterialProfile | None:
        """The row *name* lands on in this estimator's table, custom rows
        included, through the one lookup (:func:`resolve_material`)."""
        return resolve_material(name, table=self._materials)

    def estimate_from_file(
        self,
        file_path: str,
        material: str | None = None,
        electricity_rate: float = 0.12,
        printer_wattage: float = 200.0,
    ) -> CostEstimate:
        """Estimate cost from a G-code or 3MF file on disk.

        For ``.3mf`` files (including Bambu ``.gcode.3mf``), the slicer
        metadata inside the archive is used when available, which is more
        reliable than parsing the proprietary gcode within.

        Each filament is priced as the material the file was sliced for
        unless *material* names one, which then prices every filament.
        """
        if not os.path.isfile(file_path):
            raise FileNotFoundError(f"G-code file not found: {file_path}")

        # Try 3MF metadata extraction first for .3mf files, then the G-code
        # the archive prints: a 3MF whose slice figures were never filled
        # still carries the whole toolpath.
        if file_path.lower().endswith(".3mf"):
            result = self._estimate_from_3mf_metadata(
                file_path,
                material=material,
                electricity_rate=electricity_rate,
                printer_wattage=printer_wattage,
            )
            if result is not None:
                return result
            lines = _sliced_3mf_gcode_lines(file_path)
        else:
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
        material: str | None = None,
        electricity_rate: float = 0.12,
        printer_wattage: float = 200.0,
    ) -> CostEstimate:
        """Estimate cost from a list of G-code lines.

        The slicer's own ``filament used`` totals are the primary source
        when the file carries them: the slicer computed them from the
        toolpath it wrote, per extruder, and they are the figures its
        user already saw.  Kiln's own count of the extruding moves is the
        check on them, and the only source for a file with no totals; a
        count that sits more than 5% from the slicer's total is reported
        as a warning with both numbers rather than trusted silently.

        Each filament is weighed and priced by :meth:`filament_pricing`'s
        rule: as the material the file was sliced for (its
        ``filament_type``), unless *material* names one, which then prices
        every filament.  A filament the file names nothing for is priced
        as PLA, and a warning says so.
        """
        warnings: list[str] = []

        # Parse extrusion and time
        counted_e_mm = self._parse_extrusion(lines)
        comments = _comment_text(lines)
        printed = slicer_print_time(comments)
        est_time = printed.seconds if printed is not None else None

        # The slicer's own totals, read by the one reader that owns them.
        totals = slicer_filament_totals(comments)
        header_e_mm = totals.total_mm

        filament_source = "gcode_moves"
        total_e_mm = counted_e_mm
        if header_e_mm > 0:
            filament_source = "slicer_header"
            total_e_mm = header_e_mm
            gap = abs(counted_e_mm - header_e_mm) / header_e_mm
            if gap > _HEADER_DISAGREEMENT_FRACTION:
                warnings.append(
                    f"Kiln counted {counted_e_mm / 1000.0:.3f} m of extrusion in the "
                    f"moves but the slicer's own total is {header_e_mm / 1000.0:.3f} m "
                    f"({gap:.0%} apart); the slicer's figure is used"
                )
        elif total_e_mm <= 0:
            warnings.append("No extrusion commands found in G-code")

        pricing = self._gcode_filaments(
            "\n".join(line.rstrip("\r\n") for line in lines), comments, material
        )
        warnings.extend(pricing.warnings)

        # Convert E-axis mm to filament length in meters
        filament_length_m = total_e_mm / 1000.0

        # Electricity cost
        electricity_cost = 0.0
        if est_time and est_time > 0:
            hours = est_time / 3600.0
            kwh = (printer_wattage / 1000.0) * hours
            electricity_cost = kwh * electricity_rate

        # Round only at the final output — avoid rounding intermediate values
        # to prevent accumulation errors.  Round total_cost from unrounded
        # intermediates so the result is as accurate as possible.
        total_cost = pricing.cost_usd + electricity_cost

        return CostEstimate(
            file_name=file_name,
            material=pricing.material,
            filament_length_meters=round(filament_length_m, 3),
            filament_weight_grams=round(pricing.weight_g, 2),
            filament_cost_usd=round(pricing.cost_usd, 4),
            estimated_time_seconds=est_time,
            electricity_cost_usd=round(electricity_cost, 4),
            electricity_rate_kwh=electricity_rate,
            printer_wattage=printer_wattage,
            total_cost_usd=round(total_cost, 2),
            warnings=warnings,
            filament_source=filament_source,
            filaments=pricing.filaments,
            material_source=pricing.material_source,
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

        :param file_path: Path to an STL, OBJ, GLB, or 3MF mesh file.
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
            filament_source="mesh",
            cost_breakdown=cost_breakdown,
            cost_summary=cost_summary,
        )

    def _estimate_from_3mf_metadata(
        self,
        file_path: str,
        material: str | None = None,
        electricity_rate: float = 0.12,
        printer_wattage: float = 200.0,
    ) -> CostEstimate | None:
        """Extract cost data from 3MF slicer metadata (slice_info.config).

        Returns ``None`` if the archive doesn't contain usable metadata,
        allowing the caller to fall back to gcode line parsing.
        """
        found = self._slice_info_filaments(file_path, material)
        if found is None:
            return None
        pricing, est_time = found

        # Electricity cost.
        electricity_cost = 0.0
        if est_time and est_time > 0:
            hours = est_time / 3600.0
            kwh = (printer_wattage / 1000.0) * hours
            electricity_cost = kwh * electricity_rate

        total_cost = pricing.cost_usd + electricity_cost

        return CostEstimate(
            file_name=os.path.basename(file_path),
            material=pricing.material,
            filament_length_meters=round(pricing.length_mm / 1000.0, 3),
            filament_weight_grams=round(pricing.weight_g, 2),
            filament_cost_usd=round(pricing.cost_usd, 4),
            estimated_time_seconds=est_time,
            electricity_cost_usd=round(electricity_cost, 4),
            electricity_rate_kwh=electricity_rate,
            printer_wattage=printer_wattage,
            total_cost_usd=round(total_cost, 2),
            warnings=list(pricing.warnings),
            filament_source="3mf",
            filaments=pricing.filaments,
            material_source=pricing.material_source,
        )

    # -- A print file's filaments ------------------------------------------

    def filament_pricing(
        self,
        file_path: str,
        material: str | None = None,
    ) -> FilamentPricing | None:
        """What a print file's filaments weigh and cost, and nothing else.

        The filament half of :meth:`estimate_from_file`, for a door that
        needs only that: no count of the moves, no time, no electricity.
        Each filament is priced as the material the file was sliced for,
        unless *material* names one.  ``None`` when the file cannot be
        read or says nothing about its filament.
        """
        try:
            if file_path.lower().endswith(".3mf"):
                found = self._slice_info_filaments(file_path, material)
                if found is not None:
                    return found[0]
                body = "".join(_sliced_3mf_gcode_lines(file_path))
            else:
                with open(file_path, errors="replace") as fh:
                    body = fh.read()
        except (OSError, ValueError):
            return None
        comments = "\n".join(line for line in body.splitlines() if line.lstrip().startswith(";"))
        pricing = self._gcode_filaments(body, comments, material)
        return pricing if pricing.length_mm > 0 else None

    def _gcode_filaments(self, body: str, comments: str, material: str | None) -> FilamentPricing:
        """Weigh and price each filament a G-code body uses.

        Weighed by the one weigher every door uses
        (:func:`kiln.printers.bambu_3mf.filament_usage_from_gcode`): the
        slicer's own grams per filament when it wrote them, else its length
        weighed at the file's type.  A slicer that wrote only the plate's
        grams has its figure shared out as Kiln weighed the filaments.
        """
        from kiln.printers.bambu_3mf import filament_usage_from_gcode

        totals = slicer_filament_totals(comments)
        usage = filament_usage_from_gcode(body)
        grams = list(usage.grams)
        slicer_weighed = usage.source == "slicer_grams"
        if not slicer_weighed and not totals.grams and totals.total_g and sum(grams) > 0:
            share = totals.total_g / sum(grams)
            grams = [g * share for g in grams]
            slicer_weighed = True
        return _price_filaments(
            self.get_material,
            lengths_mm=usage.mm,
            grams=grams,
            file_types=slicer_filament_types(comments),
            named=material,
            slicer_weighed=slicer_weighed,
            filament_source="slicer_header" if totals.total_mm > 0 else "gcode_moves",
        )

    def _slice_info_filaments(
        self,
        file_path: str,
        material: str | None,
    ) -> tuple[FilamentPricing, int | None] | None:
        """A sliced 3MF's filaments from its ``Metadata/slice_info.config``,
        and the slicer's print time, or ``None`` when it carries no figures.

        Bambu Studio, OrcaSlicer, and compatible slicers write one
        ``<filament>`` entry per filament a plate uses, with its type,
        length (``used_m``) and grams (``used_g``), beside the plate's
        predicted time.
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

        # Each filament's length, grams and type, by tool (``id`` counts from 1).
        by_tool: dict[int, tuple[float, float, str]] = {}
        for position, fil in enumerate(plate.findall("filament")):
            try:
                used_m = float(fil.get("used_m") or 0)
                used_g = float(fil.get("used_g") or 0)
            except ValueError:
                continue
            try:
                tool = int(fil.get("id") or "") - 1
            except ValueError:
                tool = position
            by_tool[tool if tool >= 0 else position] = (
                used_m * 1000.0,
                used_g,
                (fil.get("type") or "").strip(),
            )
        if sum(mm for mm, _, _ in by_tool.values()) <= 0 and sum(g for _, g, _ in by_tool.values()) <= 0:
            return None

        size = max(by_tool) + 1
        lengths_mm = [0.0] * size
        grams = [0.0] * size
        types = [""] * size
        for tool, (mm, g, word) in by_tool.items():
            lengths_mm[tool], grams[tool], types[tool] = mm, g, word
        pricing = _price_filaments(
            self.get_material,
            lengths_mm=lengths_mm,
            grams=grams,
            file_types=tuple(types),
            named=material,
            slicer_weighed=True,
            filament_source="3mf",
        )

        est_time: int | None = None
        for md in plate.findall("metadata"):
            if md.get("key") == "prediction":
                with contextlib.suppress(ValueError, TypeError):
                    est_time = int(md.get("value") or "")
                break
        return pricing, est_time

    def _parse_extrusion(self, lines: list[str]) -> float:
        """Total filament laid down, in mm, counted from the moves.

        The one counter every door uses
        (:func:`kiln.gcode.extruded_mm_per_tool`): it matches the slicer's
        own ``filament used`` total on every real file it was measured
        against, and the weight written for a printer's screen is counted
        the same way.
        """
        return float(sum(extruded_mm_per_tool(lines)))
