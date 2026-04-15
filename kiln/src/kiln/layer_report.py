"""Per-layer structural breakdown for a sliced gcode file.

Answers the question: "given this gcode, what does each layer actually
contain — solid top, solid bottom, perimeter, infill, bridge, tool
change — and does the structure make sense for printing?"

Meant as a **pre-print validation lens** that sits alongside
``validate_and_prepare``'s mesh-level quality gate.  Where
``validate_and_prepare`` asks "will my MESH print?", this module
asks "will my LAYERS be what I expect?" — catching issues like a
missing top-solid band, a Z-gap mid-part, or a thermal transition
landing in a weird spot.

Streaming O(n) parse over the gcode file.  No heightmap, no mesh
lookups, no external tools.  Designed to be the primitive that both
Kiln (public / free-tier) and kiln-pro build on:

    - Public Kiln (this module) — single-gcode layer breakdown.
    - kiln-pro (``kiln_pro.preview.layer_impact``) — uses this as
      a building block, adds the dual-gcode diff + depth-achievement
      check + mid-print-specific safety flags.

Slicer support: anything that emits the conventional comment
patterns PrusaSlicer / SuperSlicer / OrcaSlicer / Bambu Studio /
Cura all honour — ``;LAYER_CHANGE``, ``;Z:{value}``, ``;TYPE:…``,
``M104``/``M140`` setpoints.  Works without perfect coverage — a
gcode that only has ``G1 Z`` transitions (no ;LAYER_CHANGE) still
produces a useful layer count.

Used by the MCP tool ``analyze_layers`` registered in the utility
plugin surface.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any

_logger = logging.getLogger(__name__)


# ``;TYPE:<name>`` tokens seen across all major slicers.  The normalised
# categories below collapse slicer-specific synonyms into five buckets
# the report surfaces: ``perimeter``, ``infill``, ``top_solid``,
# ``bottom_solid``, ``bridge``.  Unknown types fall back to ``other``.
_TYPE_NORMALIZE = {
    # PrusaSlicer / SuperSlicer / OrcaSlicer (OrcaSlicer reuses most PS tokens)
    "external perimeter": "perimeter",
    "perimeter": "perimeter",
    "overhang perimeter": "perimeter",
    "top solid infill": "top_solid",
    "top surface": "top_solid",           # also emitted by Bambu Studio
    "solid infill": "top_solid",          # usually supports the top
    "internal solid infill": "top_solid",  # OrcaSlicer
    "internal infill": "infill",
    "infill": "infill",
    "sparse infill": "infill",            # also emitted by Bambu Studio
    "bridge infill": "bridge",
    "bridge": "bridge",
    "skirt/brim": "other",
    "skirt": "other",
    "brim": "other",
    "support material": "other",
    "support": "other",                   # also emitted by Cura
    "support material interface": "other",
    # Bambu Studio (keys not shared with PrusaSlicer)
    "outer wall": "perimeter",
    "inner wall": "perimeter",
    "bottom surface": "bottom_solid",
    # Cura (keys not shared with PrusaSlicer)
    "wall-outer": "perimeter",
    "wall-inner": "perimeter",
    "skin": "top_solid",
    "fill": "infill",
}

_LAYER_CHANGE_RE = re.compile(r"^\s*;\s*(LAYER_CHANGE|CHANGE_LAYER)\b", re.IGNORECASE)
_LAYER_Z_RE = re.compile(r"^\s*;\s*Z:\s*(-?\d*\.?\d+)", re.IGNORECASE)
_TYPE_RE = re.compile(r"^\s*;\s*TYPE:\s*(.+?)\s*$", re.IGNORECASE)
_G1_Z_RE = re.compile(r"^\s*G[01]\b.*\bZ(-?\d*\.?\d+)", re.IGNORECASE)
_G1_E_RE = re.compile(r"^\s*G[01]\b.*\bE(-?\d*\.?\d+)", re.IGNORECASE)
_XY_RE = re.compile(r"\bX(-?\d*\.?\d+)\s+Y(-?\d*\.?\d+)", re.IGNORECASE)
_M104_RE = re.compile(r"^\s*M104\b.*\bS(-?\d*\.?\d+)", re.IGNORECASE)
_M140_RE = re.compile(r"^\s*M140\b.*\bS(-?\d*\.?\d+)", re.IGNORECASE)
_T_RE = re.compile(r"^\s*T(\d+)\b", re.IGNORECASE)


@dataclass
class LayerSummary:
    """Per-layer breakdown surfaced in the report."""

    index: int                  # 1-indexed
    z_mm: float | None = None
    # Normalised type label for this layer.  When a single layer
    # contains multiple ;TYPE: markers (common — perimeter then infill)
    # we pick the dominant one by extrusion-move count, with a tie-
    # break toward the more "top-facing" category so the structural
    # sanity checks see the layer's visible role.
    type: str = "other"
    # Count of ;TYPE: markers seen per normalised category — useful
    # when a caller wants the full mix rather than the dominant.
    type_counts: dict[str, int] = field(default_factory=dict)
    # Move counts.  ``extrude_moves`` excludes retract/prime moves
    # (E-only lines) so the count reflects geometry-laying travels.
    extrude_moves: int = 0
    travel_moves: int = 0
    # Active tool at end of layer (for multi-material prints).
    tool_index: int | None = None
    # XY envelope for THIS layer's extrusions.  None when the layer
    # has no extrusion moves at all (Z-lift-only layers, etc.).
    xy_bounds: tuple[float, float, float, float] | None = None


@dataclass
class LayerReport:
    """Structured report returned by :func:`analyze_layers`."""

    gcode_path: str
    total_layers: int = 0
    z_min_mm: float | None = None
    z_max_mm: float | None = None
    # Detected via mode over consecutive-layer Z deltas.  Falls back
    # to 0.2 when the file has fewer than 2 layers with recognised Z.
    layer_height_mm: float = 0.2
    layers: list[LayerSummary] = field(default_factory=list)
    # Overall XY envelope across every extrusion in the file.  Useful
    # for bed-fit sanity checks before upload.
    xy_bounds: tuple[float, float, float, float] | None = None
    # Thermal setpoints observed in the file header / preamble.
    hotend_setpoint_c: float | None = None
    bed_setpoint_c: float | None = None
    # Did the file set a hotend + bed target at all?  Callers can
    # refuse to upload a gcode that forgot to heat the printer.
    hotend_set: bool = False
    bed_set: bool = False
    # Tools referenced in the file (tool_index -> extrude_moves).
    tools_used: dict[int, int] = field(default_factory=dict)
    # Structural sanity flags — set by the summary pass below.  Each
    # ``False`` flag triggers a warning string in ``warnings``.
    has_top_solid: bool = False
    has_bottom_solid: bool = False
    has_perimeter: bool = False
    z_continuous: bool = True  # no unexpected Z gaps between layers
    # Human-readable warnings for user-facing envelopes.
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        from dataclasses import asdict
        return asdict(self)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _normalise_type(tag: str) -> str:
    """Slicer-specific ;TYPE: label → normalised category."""
    return _TYPE_NORMALIZE.get(tag.strip().lower(), "other")


def _dominant_type(counts: dict[str, int]) -> str:
    """Pick the most-frequent normalised type, with a tie-break
    toward the more top-facing category (top_solid > bottom_solid >
    perimeter > bridge > infill > other)."""
    if not counts:
        return "other"
    priority = {
        "top_solid": 5,
        "bottom_solid": 4,
        "perimeter": 3,
        "bridge": 2,
        "infill": 1,
        "other": 0,
    }
    # Sort by (-count, -priority) so highest count wins, priority
    # breaks ties.
    best = sorted(
        counts.items(),
        key=lambda kv: (-kv[1], -priority.get(kv[0], 0)),
    )
    return best[0][0]


def _detect_layer_height(layers: list[LayerSummary]) -> float:
    """Mode of consecutive-layer Z deltas.  Robust to variable-layer
    output where the top layers are thinner than the bottom — most-
    common delta wins.  Returns 0.2 when we can't infer."""
    zs = [L.z_mm for L in layers if L.z_mm is not None]
    if len(zs) < 2:
        return 0.2
    zs_sorted = sorted(zs)
    deltas = [
        round(zs_sorted[i] - zs_sorted[i - 1], 3)
        for i in range(1, len(zs_sorted))
        if zs_sorted[i] > zs_sorted[i - 1]
    ]
    if not deltas:
        return 0.2
    counts: dict[float, int] = {}
    for d in deltas:
        counts[d] = counts.get(d, 0) + 1
    best = min(counts, key=lambda k: (-counts[k], k))
    return best


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def analyze_layers(gcode_path: str) -> LayerReport:
    """Parse ``gcode_path`` into a :class:`LayerReport`.

    Single streaming pass — O(n) in file size, O(layer_count) memory.

    :param gcode_path: Absolute or relative path to a sliced gcode file.
    :raises FileNotFoundError: if the file is missing.
    :raises ValueError: if the file contains zero G1 moves.
    """
    if not os.path.isfile(gcode_path):
        raise FileNotFoundError(f"Gcode not found: {gcode_path}")

    report = LayerReport(gcode_path=os.path.abspath(gcode_path))

    # Streaming state.
    current_layer: LayerSummary | None = None
    current_z: float | None = None
    cur_tool: int = 0
    cur_type: str = "other"
    saw_g1 = False

    # XY envelope accumulators — file-wide + per-layer.
    file_xmin = float("inf")
    file_ymin = float("inf")
    file_xmax = float("-inf")
    file_ymax = float("-inf")
    layer_xmin = float("inf")
    layer_ymin = float("inf")
    layer_xmax = float("-inf")
    layer_ymax = float("-inf")

    def close_layer():
        """Commit the current layer to the report."""
        nonlocal layer_xmin, layer_ymin, layer_xmax, layer_ymax
        if current_layer is None:
            return
        if layer_xmax > layer_xmin and layer_ymax > layer_ymin:
            current_layer.xy_bounds = (
                layer_xmin, layer_ymin, layer_xmax, layer_ymax,
            )
        current_layer.type = _dominant_type(current_layer.type_counts)
        current_layer.tool_index = cur_tool
        report.layers.append(current_layer)
        # Reset layer envelope for the next layer.
        layer_xmin = float("inf")
        layer_ymin = float("inf")
        layer_xmax = float("-inf")
        layer_ymax = float("-inf")

    with open(gcode_path, encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.rstrip("\n")

            # Layer change boundary.
            if _LAYER_CHANGE_RE.match(line):
                close_layer()
                current_layer = LayerSummary(index=len(report.layers) + 1)
                continue

            # Explicit Z annotation on layer start.
            m = _LAYER_Z_RE.match(line)
            if m:
                z = float(m.group(1))
                current_z = z
                if current_layer is not None and current_layer.z_mm is None:
                    current_layer.z_mm = z
                continue

            # ;TYPE: tag.
            m = _TYPE_RE.match(line)
            if m:
                cur_type = _normalise_type(m.group(1))
                if current_layer is not None:
                    current_layer.type_counts[cur_type] = (
                        current_layer.type_counts.get(cur_type, 0) + 1
                    )
                continue

            # Thermal setpoints — first occurrence wins (the file's
            # nominal target).  Track whether we set any temp.
            m = _M104_RE.match(line)
            if m:
                report.hotend_set = True
                if report.hotend_setpoint_c is None:
                    report.hotend_setpoint_c = float(m.group(1))
                continue
            m = _M140_RE.match(line)
            if m:
                report.bed_set = True
                if report.bed_setpoint_c is None:
                    report.bed_setpoint_c = float(m.group(1))
                continue

            # Tool change (T0, T1, ...).
            m = _T_RE.match(line)
            if m:
                cur_tool = int(m.group(1))
                continue

            # G0/G1 motion — track extrudes, travels, Z, XY envelope.
            if line.lstrip().startswith(("G0", "G1")):
                saw_g1 = True
                # Z update within layer (or implicit layer increment
                # when ;LAYER_CHANGE is absent).
                m = _G1_Z_RE.match(line)
                if m:
                    new_z = float(m.group(1))
                    # Implicit layer increment when we see a new Z
                    # without having opened a layer yet AND the file
                    # doesn't use ;LAYER_CHANGE.  Skip tiny Z moves
                    # (<0.05mm — those are Z-hops, not layer changes).
                    if (
                        current_layer is None
                        or (
                            current_z is not None
                            and new_z > current_z + 0.05
                        )
                    ):
                        close_layer()
                        current_layer = LayerSummary(index=len(report.layers) + 1)
                        current_layer.z_mm = new_z
                    current_z = new_z

                # XY envelope.
                xy = _XY_RE.search(line)
                if xy:
                    x = float(xy.group(1))
                    y = float(xy.group(2))
                    if x < file_xmin:
                        file_xmin = x
                    if x > file_xmax:
                        file_xmax = x
                    if y < file_ymin:
                        file_ymin = y
                    if y > file_ymax:
                        file_ymax = y
                    if x < layer_xmin:
                        layer_xmin = x
                    if x > layer_xmax:
                        layer_xmax = x
                    if y < layer_ymin:
                        layer_ymin = y
                    if y > layer_ymax:
                        layer_ymax = y

                # Extrude vs travel classification.
                has_e = _G1_E_RE.match(line)
                if current_layer is not None:
                    if has_e and xy:
                        # Only count as extrude when E is positive AND
                        # XY moves — retracts/primes (E without XY)
                        # aren't counted against the extrude metric.
                        try:
                            e_val = float(has_e.group(1))
                            if e_val > 0:
                                current_layer.extrude_moves += 1
                                report.tools_used[cur_tool] = (
                                    report.tools_used.get(cur_tool, 0) + 1
                                )
                            else:
                                # Retract — not an extrude, not a travel.
                                pass
                        except ValueError:
                            pass
                    elif xy:
                        current_layer.travel_moves += 1

    # Commit the final layer (no ;LAYER_CHANGE at EOF).
    close_layer()

    if not saw_g1:
        raise ValueError(
            f"{gcode_path} contains zero G0/G1 moves — not a gcode file."
        )

    # Aggregate.
    report.total_layers = len(report.layers)
    zs = [L.z_mm for L in report.layers if L.z_mm is not None]
    if zs:
        report.z_min_mm = min(zs)
        report.z_max_mm = max(zs)
    report.layer_height_mm = _detect_layer_height(report.layers)
    if file_xmax > file_xmin and file_ymax > file_ymin:
        report.xy_bounds = (file_xmin, file_ymin, file_xmax, file_ymax)

    # Structural sanity flags + warnings.
    for L in report.layers:
        if L.type == "top_solid":
            report.has_top_solid = True
        if L.type == "bottom_solid":
            report.has_bottom_solid = True
        if L.type == "perimeter":
            report.has_perimeter = True

    # Z-continuity: consecutive layers should step up by roughly
    # layer_height.  A gap > 2x layer_height flags a discontinuity.
    for i in range(1, len(report.layers)):
        prev_z = report.layers[i - 1].z_mm
        this_z = report.layers[i].z_mm
        if prev_z is None or this_z is None:
            continue
        delta = this_z - prev_z
        if delta > 2 * report.layer_height_mm + 0.05:
            report.z_continuous = False
            report.warnings.append(
                f"Z_GAP: layer {report.layers[i].index} jumps "
                f"{delta:.2f}mm from layer {report.layers[i - 1].index} "
                f"(expected ~{report.layer_height_mm:.2f}mm).  "
                f"Possible truncation or corrupt gcode."
            )
            break  # one warning is enough

    # Minimum structural expectations for a printable object:
    if not report.hotend_set:
        report.warnings.append(
            "NO_HOTEND_SETPOINT: no M104 found.  Printer may not "
            "heat the nozzle before printing."
        )
    if not report.bed_set:
        report.warnings.append(
            "NO_BED_SETPOINT: no M140 found.  Printer may not heat "
            "the bed before printing."
        )
    if not report.has_perimeter:
        report.warnings.append(
            "NO_PERIMETER: no perimeter-tagged layers detected.  "
            "The gcode may be skirt/brim only or the slicer used "
            "non-standard ;TYPE: labels."
        )
    if not report.has_top_solid and report.total_layers >= 3:
        report.warnings.append(
            "NO_TOP_SOLID: no top-solid-tagged layers.  The printed "
            "part's top surface may expose infill — check slicer's "
            "top-solid-layers setting."
        )

    _logger.info(
        "analyze_layers: %s layers=%d z=%.2f..%.2f mm=%.2f h_set=%s "
        "b_set=%s",
        gcode_path,
        report.total_layers,
        report.z_min_mm if report.z_min_mm is not None else 0,
        report.z_max_mm if report.z_max_mm is not None else 0,
        report.layer_height_mm,
        report.hotend_set,
        report.bed_set,
    )
    return report


__all__ = ["LayerReport", "LayerSummary", "analyze_layers"]
