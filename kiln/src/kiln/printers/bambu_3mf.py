"""Bambu Lab 3MF packaging for PrusaSlicer gcode.

Wraps PrusaSlicer-generated gcode with BambuStudio's proprietary
start/end gcode and packages everything as a Bambu-compatible 3MF
file ready for upload and printing.

The Bambu A1 (and other Bambu printers) require a specific proprietary
initialization sequence in the start gcode for the extruder motor to
respond to E commands.  Without it, ``G1 E`` commands are silently
ignored — the head moves but nothing extrudes.  This module provides
that sequence.

The proven pipeline:
    1. PrusaSlicer slices the model with ``--use-relative-e-distances``
       and empty start/end gcode.
    2. This module wraps the gcode body with the BambuStudio A1 start
       gcode (~620 lines, including M620 M motor enable, AMS load,
       nozzle flush, extrusion calibration, bed leveling) and end gcode
       (~150 lines, AMS retract, cooldown, finish sound).
    3. Layer tracking commands (M73 L, M991 S0 P0, M73 P/R) are
       injected at each PrusaSlicer ``;LAYER_CHANGE`` marker.
    4. Everything is packaged as a Bambu 3MF with proper metadata.

Tested and verified on the Bambu Lab A1 Combo (firmware 01.08.03.00).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data file paths
# ---------------------------------------------------------------------------

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_A1_START_GCODE_PATH = _DATA_DIR / "bambu_a1_start_gcode.gcode"
_A1_END_GCODE_PATH = _DATA_DIR / "bambu_a1_end_gcode.gcode"

# Lazy-loaded singletons for gcode templates.
_a1_start_gcode: str | None = None
_a1_end_gcode: str | None = None


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class BambuPrintSettings:
    """Print-specific settings for Bambu 3MF building.

    All temperatures are in degrees Celsius.  Defaults are for PLA on
    the Bambu A1 with a 0.4 mm nozzle.
    """

    hotend_temp: int = 220
    bed_temp: int = 65
    filament_type: str = "PLA"
    filament_color: str = "#FFFFFF"
    nozzle_diameter: float = 0.4
    layer_height: float = 0.2
    bed_type: str = "textured_plate"
    model_name: str = "model"

    def to_dict(self) -> dict[str, Any]:
        return {
            "hotend_temp": self.hotend_temp,
            "bed_temp": self.bed_temp,
            "filament_type": self.filament_type,
            "filament_color": self.filament_color,
            "nozzle_diameter": self.nozzle_diameter,
            "layer_height": self.layer_height,
            "bed_type": self.bed_type,
            "model_name": self.model_name,
        }


@dataclass
class Bambu3MFResult:
    """Result of building a Bambu 3MF."""

    output_path: str
    total_layers: int
    max_z: float
    file_size: int
    md5: str
    est_print_time_sec: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "output_path": self.output_path,
            "total_layers": self.total_layers,
            "max_z": self.max_z,
            "file_size": self.file_size,
            "md5": self.md5,
            "est_print_time_sec": self.est_print_time_sec,
        }


# ---------------------------------------------------------------------------
# Template loading (lazy singletons)
# ---------------------------------------------------------------------------


def _load_a1_start_gcode() -> str:
    """Load the A1 start gcode template."""
    global _a1_start_gcode  # noqa: PLW0603
    if _a1_start_gcode is None:
        if not _A1_START_GCODE_PATH.is_file():
            msg = f"Bambu A1 start gcode not found: {_A1_START_GCODE_PATH}"
            raise FileNotFoundError(msg)
        _a1_start_gcode = _A1_START_GCODE_PATH.read_text(encoding="utf-8")
    return _a1_start_gcode


def _load_a1_end_gcode() -> str:
    """Load the A1 end gcode template."""
    global _a1_end_gcode  # noqa: PLW0603
    if _a1_end_gcode is None:
        if not _A1_END_GCODE_PATH.is_file():
            msg = f"Bambu A1 end gcode not found: {_A1_END_GCODE_PATH}"
            raise FileNotFoundError(msg)
        _a1_end_gcode = _A1_END_GCODE_PATH.read_text(encoding="utf-8")
    return _a1_end_gcode


# ---------------------------------------------------------------------------
# Template resolution
# ---------------------------------------------------------------------------

# Fixed temperatures in the A1 start gcode that must NOT be replaced:
#   140°C — initial nozzle preheat for bed leveling
#   170°C — nozzle wipe temperature
#   250°C — filament flush temperature
#   25°C  — cooldown check
# Only 220°C (PLA print temp) and 65°C (PLA bed temp) are parametric.


def _resolve_start_gcode(
    template: str,
    *,
    hotend_temp: int = 220,
    bed_temp: int = 65,
    filament_type: str = "PLA",
) -> str:
    """Resolve the A1 start gcode template with print-specific values.

    Replaces PLA-default temperatures (220°C hotend, 65°C bed) and
    filament type with the actual print values.  Fixed init temperatures
    (140°C preheat, 250°C flush, 170°C wipe) are preserved.
    """
    lines = template.split("\n")
    resolved: list[str] = []

    for line in lines:
        stripped = line.strip()

        # Replace hotend temp: M104/M109 S220 → S{hotend_temp}
        if hotend_temp != 220 and (
            stripped.startswith("M104 S220") or stripped.startswith("M109 S220")
        ):
            line = line.replace("S220", f"S{hotend_temp}")

        # Replace bed temp: M140/M190 S65 → S{bed_temp}
        elif bed_temp != 65 and (
            stripped.startswith("M140 S65") or stripped.startswith("M190 S65")
        ):
            line = line.replace("S65", f"S{bed_temp}")

        # Replace filament type (skip UNKNOWN lines — fixed for AMS switching)
        elif filament_type != "PLA" and "set_filament_type:PLA" in line:
            line = line.replace("set_filament_type:PLA", f"set_filament_type:{filament_type}")

        resolved.append(line)

    return "\n".join(resolved)


def _resolve_end_gcode(
    template: str,
    *,
    max_z: float = 65.0,
) -> str:
    """Resolve the A1 end gcode template with print-specific values.

    Adjusts the safe Z-move height based on the actual print height.
    The first ``G1 Z... F900`` command is the safe-move after the last
    layer — it needs to clear the print.
    """
    safe_z = max_z + 5.0
    return re.sub(
        r"(G1 Z)\d+\.?\d*( F900)",
        rf"\g<1>{safe_z:.1f}\2",
        template,
        count=1,
    )


# ---------------------------------------------------------------------------
# Gcode post-processing
# ---------------------------------------------------------------------------


def _count_layers(gcode_body: str) -> int:
    """Count ``;LAYER_CHANGE`` markers in PrusaSlicer gcode."""
    return len(re.findall(r"^;LAYER_CHANGE", gcode_body, re.MULTILINE))


def _find_max_z(gcode_body: str) -> float:
    """Find the maximum Z height from PrusaSlicer ``;Z:`` comments."""
    z_heights = re.findall(r";Z:(\d+\.?\d*)", gcode_body)
    return max(float(z) for z in z_heights) if z_heights else 10.0


def _postprocess_prusa_body(
    gcode_body: str,
    *,
    total_layers: int,
    est_time_sec: int,
) -> str:
    """Post-process PrusaSlicer gcode body for Bambu firmware compatibility.

    1. Strips PrusaSlicer's own init commands (M83, G28, M104, etc.)
       since the BambuStudio start gcode handles machine initialization.
    2. Injects Bambu-specific layer tracking at each ``;LAYER_CHANGE``:
       - ``M73 L{n}`` — layer number for firmware display
       - ``M991 S0 P0`` — notify firmware of layer change
       - ``M73 P{pct} R{min}`` — progress percentage and remaining time
    """
    body_lines = gcode_body.split("\n")

    # Strip PrusaSlicer init commands before the first layer.
    _skip_prefixes = (
        "M83", "M82", "G21", "G90", "G92", "M107",
        "M104", "M140", "M190", "M109", "G28",
    )
    cleaned: list[str] = []
    in_header = True
    for line in body_lines:
        stripped = line.strip()
        if in_header:
            if stripped.startswith((";BEFORE_LAYER_CHANGE", ";LAYER_CHANGE")):
                in_header = False
                cleaned.append(line)
            elif stripped.startswith(";") or stripped == "":
                cleaned.append(line)
            elif stripped.startswith(_skip_prefixes):
                continue  # Skip — Bambu start gcode handles these
            else:
                in_header = False
                cleaned.append(line)
        else:
            cleaned.append(line)

    # Inject Bambu layer tracking at each ;LAYER_CHANGE
    layer_num = 0
    processed: list[str] = []
    for line in cleaned:
        if line.strip() == ";LAYER_CHANGE":
            layer_num += 1
            processed.append(line)
            processed.append(
                f"; layer num/total_layer_count: {layer_num}/{total_layers}"
            )
            processed.append("; update layer progress")
            processed.append(f"M73 L{layer_num}")
            processed.append("M991 S0 P0 ;notify layer change")
            pct = min(int(layer_num * 100 / total_layers), 99)
            remaining_sec = max(
                60, int((total_layers - layer_num) * est_time_sec / total_layers)
            )
            remaining_min = max(1, remaining_sec // 60)
            processed.append(f"M73 P{pct} R{remaining_min}")
            continue
        processed.append(line)

    return "\n".join(processed)


# ---------------------------------------------------------------------------
# Gcode assembly
# ---------------------------------------------------------------------------


def _build_gcode_header(
    *,
    total_layers: int,
    max_z: float,
    est_print_time_sec: int,
    filament_type: str = "PLA",
    nozzle_diameter: float = 0.4,
    hotend_temp: int = 220,
    bed_temp: int = 65,
) -> str:
    """Build the Bambu-compatible gcode header block."""
    est_h = est_print_time_sec // 3600
    est_m = (est_print_time_sec % 3600) // 60
    est_s = est_print_time_sec % 60

    return (
        f"; HEADER_BLOCK_START\n"
        f"; BambuStudio 02.05.00.66\n"
        f"; model printing time: {est_h}h {est_m}m {est_s}s; "
        f"total estimated time: {est_h}h {est_m + 5}m 0s\n"
        f"; total layer number: {total_layers}\n"
        f"; filament_density: 1.24\n"
        f"; filament_diameter: 1.75\n"
        f"; max_z_height: {max_z:.2f}\n"
        f"; filament: 1\n"
        f"; HEADER_BLOCK_END\n"
        f"\n"
        f"; CONFIG_BLOCK_START\n"
        f"; filament_type = {filament_type}\n"
        f"; nozzle_diameter = {nozzle_diameter}\n"
        f"; bed_temperature = {bed_temp}\n"
        f"; temperature = {hotend_temp}\n"
        f"; CONFIG_BLOCK_END\n"
        f"\n"
    )


# ---------------------------------------------------------------------------
# 3MF metadata builders
# ---------------------------------------------------------------------------


def _build_slice_info(
    *,
    total_layers: int,
    est_print_time_sec: int,
    filament_type: str = "PLA",
    filament_color: str = "#FFFFFF",
    nozzle_diameter: float = 0.4,
    model_name: str = "model",
    first_layer_time: float = 60.0,
) -> str:
    """Build the ``slice_info.config`` XML for the 3MF."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<config>\n"
        "  <header>\n"
        '    <header_item key="X-BBL-Client-Type" value="slicer"/>\n'
        '    <header_item key="X-BBL-Client-Version" value="02.05.00.66"/>\n'
        "  </header>\n"
        "  <plate>\n"
        '    <metadata key="index" value="1"/>\n'
        '    <metadata key="extruder_type" value="0"/>\n'
        '    <metadata key="nozzle_volume_type" value="0"/>\n'
        '    <metadata key="printer_model_id" value="N2S"/>\n'
        f'    <metadata key="nozzle_diameters" value="{nozzle_diameter}"/>\n'
        '    <metadata key="timelapse_type" value="0"/>\n'
        f'    <metadata key="prediction" value="{est_print_time_sec}"/>\n'
        '    <metadata key="weight" value="0.00"/>\n'
        f'    <metadata key="first_layer_time" value="{first_layer_time:.1f}"/>\n'
        '    <metadata key="outside" value="false"/>\n'
        '    <metadata key="support_used" value="false"/>\n'
        '    <metadata key="label_object_enabled" value="false"/>\n'
        '    <metadata key="filament_maps" value="1"/>\n'
        '    <metadata key="limit_filament_maps" value="0"/>\n'
        f'    <object identify_id="1" name="{model_name}" skipped="false" />\n'
        f'    <filament id="1" tray_info_idx="GFL99" type="{filament_type}" '
        f'color="{filament_color}" used_m="0.00" used_g="0.00" '
        f'used_for_object="true" used_for_support="false" group_id="0" '
        f'nozzle_diameter="{nozzle_diameter:.2f}" volume_type="Standard"/>\n'
        "    <layer_filament_lists>\n"
        f'      <layer_filament_list filament_list="0" '
        f'layer_ranges="0 {total_layers - 1}" />\n'
        "    </layer_filament_lists>\n"
        "  </plate>\n"
        "</config>"
    )


def _build_plate_json(
    *,
    filament_color: str = "#FFFFFF",
    nozzle_diameter: float = 0.4,
    bed_type: str = "textured_plate",
    first_layer_time: float = 60.0,
) -> str:
    """Build the ``plate_1.json`` metadata."""
    data = {
        "bbox_all": [78, 78, 178, 178],
        "bbox_objects": [],
        "bed_type": bed_type,
        "filament_colors": [filament_color],
        "filament_ids": [0],
        "first_extruder": 0,
        "first_layer_time": first_layer_time,
        "is_seq_print": False,
        "nozzle_diameter": nozzle_diameter,
        "version": 2,
    }
    return json.dumps(data)


# ---------------------------------------------------------------------------
# Static 3MF boilerplate
# ---------------------------------------------------------------------------

_CONTENT_TYPES_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">\n'
    '  <Default Extension="gcode" ContentType="text/x.gcode"/>\n'
    '  <Default Extension="model" ContentType='
    '"application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>\n'
    '  <Default Extension="png" ContentType="image/png"/>\n'
    '  <Default Extension="config" ContentType="text/xml"/>\n'
    '  <Default Extension="json" ContentType="application/json"/>\n'
    "</Types>"
)

_RELS_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
    '  <Relationship Target="/3D/3dmodel.model" Id="rel-1" '
    'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>\n'
    '  <Relationship Target="/Metadata/plate_1.gcode" Id="rel-2" '
    'Type="http://schemas.bambulab.com/package/2021/gcode"/>\n'
    '  <Relationship Target="/Metadata/slice_info.config" Id="rel-3" '
    'Type="http://schemas.bambulab.com/package/2021/slice-info"/>\n'
    "</Relationships>"
)

_MODEL_SETTINGS_RELS = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
    "</Relationships>"
)

# Minimal 3D model placeholder — a 1 mm cube at origin.
# The printer only reads the gcode; geometry is for BambuStudio UI only.
_MINIMAL_3D_MODEL = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<model unit="millimeter" '
    'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">\n'
    "  <resources>\n"
    '    <object id="1" type="model">\n'
    "      <mesh>\n"
    "        <vertices>\n"
    '          <vertex x="0" y="0" z="0"/>\n'
    '          <vertex x="1" y="0" z="0"/>\n'
    '          <vertex x="1" y="1" z="0"/>\n'
    '          <vertex x="0" y="1" z="0"/>\n'
    '          <vertex x="0" y="0" z="1"/>\n'
    '          <vertex x="1" y="0" z="1"/>\n'
    '          <vertex x="1" y="1" z="1"/>\n'
    '          <vertex x="0" y="1" z="1"/>\n'
    "        </vertices>\n"
    "        <triangles>\n"
    '          <triangle v1="0" v2="1" v3="2"/>\n'
    '          <triangle v1="0" v2="2" v3="3"/>\n'
    '          <triangle v1="4" v2="6" v3="5"/>\n'
    '          <triangle v1="4" v2="7" v3="6"/>\n'
    '          <triangle v1="0" v2="4" v3="5"/>\n'
    '          <triangle v1="0" v2="5" v3="1"/>\n'
    '          <triangle v1="2" v2="6" v3="7"/>\n'
    '          <triangle v1="2" v2="7" v3="3"/>\n'
    '          <triangle v1="0" v2="7" v3="4"/>\n'
    '          <triangle v1="0" v2="3" v3="7"/>\n'
    '          <triangle v1="1" v2="5" v3="6"/>\n'
    '          <triangle v1="1" v2="6" v3="2"/>\n'
    "        </triangles>\n"
    "      </mesh>\n"
    "    </object>\n"
    "  </resources>\n"
    "  <build>\n"
    '    <item objectid="1"/>\n'
    "  </build>\n"
    "</model>"
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_bambu_3mf(
    gcode_body: str,
    output_path: str,
    *,
    settings: BambuPrintSettings | None = None,
    source_3mf_path: str | None = None,
) -> Bambu3MFResult:
    """Build a Bambu-compatible 3MF from PrusaSlicer gcode body.

    Wraps the raw PrusaSlicer gcode with BambuStudio's proprietary
    start/end gcode and packages everything as a 3MF file.

    :param gcode_body: Raw gcode from PrusaSlicer (sliced with
        ``--use-relative-e-distances`` and empty start/end gcode).
    :param output_path: Path for the output 3MF file.
    :param settings: Print settings (temps, filament, etc.).
    :param source_3mf_path: Optional source 3MF to extract thumbnails
        and 3D model geometry from.
    :returns: :class:`Bambu3MFResult` with output path and metadata.
    :raises FileNotFoundError: If the start/end gcode data files are missing.
    :raises ValueError: If the gcode body has no layer changes.
    """
    if settings is None:
        settings = BambuPrintSettings()

    # Analyze the gcode body.
    total_layers = _count_layers(gcode_body)
    if total_layers == 0:
        msg = "Gcode body has no ;LAYER_CHANGE markers — cannot build 3MF."
        raise ValueError(msg)

    max_z = _find_max_z(gcode_body)
    est_time_sec = total_layers * 30  # ~30 s/layer average
    est_minutes = max(1, est_time_sec // 60)

    logger.info(
        "Building Bambu 3MF: %d layers, max_z=%.1f, est=%dm",
        total_layers,
        max_z,
        est_minutes,
    )

    # Load and resolve templates.
    start_gcode = _resolve_start_gcode(
        _load_a1_start_gcode(),
        hotend_temp=settings.hotend_temp,
        bed_temp=settings.bed_temp,
        filament_type=settings.filament_type,
    )
    end_gcode = _resolve_end_gcode(
        _load_a1_end_gcode(),
        max_z=max_z,
    )

    # Post-process the PrusaSlicer body.
    processed_body = _postprocess_prusa_body(
        gcode_body,
        total_layers=total_layers,
        est_time_sec=est_time_sec,
    )

    # Build the header.
    header = _build_gcode_header(
        total_layers=total_layers,
        max_z=max_z,
        est_print_time_sec=est_time_sec,
        filament_type=settings.filament_type,
        nozzle_diameter=settings.nozzle_diameter,
        hotend_temp=settings.hotend_temp,
        bed_temp=settings.bed_temp,
    )

    # Assemble complete gcode.
    complete_gcode = header + start_gcode + "\n" + processed_body + "\n" + end_gcode

    # Build metadata.
    gcode_bytes = complete_gcode.encode("utf-8")
    gcode_md5 = hashlib.md5(gcode_bytes).hexdigest()  # noqa: S324

    slice_info = _build_slice_info(
        total_layers=total_layers,
        est_print_time_sec=est_time_sec,
        filament_type=settings.filament_type,
        filament_color=settings.filament_color,
        nozzle_diameter=settings.nozzle_diameter,
        model_name=settings.model_name,
    )
    plate_json = _build_plate_json(
        filament_color=settings.filament_color,
        nozzle_diameter=settings.nozzle_diameter,
        bed_type=settings.bed_type,
    )
    model_settings = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<config>\n"
        '  <object id="1">\n'
        f'    <metadata key="name" value="{settings.model_name}"/>\n'
        "  </object>\n"
        "</config>"
    )

    # Extract thumbnails and geometry from source 3MF if available.
    thumbnails: dict[str, bytes] = {}
    model_data: str = _MINIMAL_3D_MODEL
    if source_3mf_path and os.path.isfile(source_3mf_path):
        try:
            with zipfile.ZipFile(source_3mf_path) as zf:
                for name in zf.namelist():
                    if name.startswith("Metadata/") and name.endswith(".png"):
                        thumbnails[name] = zf.read(name)
                    elif name == "3D/3dmodel.model":
                        model_data = zf.read(name).decode("utf-8")
        except (zipfile.BadZipFile, KeyError):
            logger.warning(
                "Could not extract thumbnails from %s", source_3mf_path
            )

    # Build the 3MF.
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES_XML)
        zf.writestr("_rels/.rels", _RELS_XML)
        zf.writestr("3D/3dmodel.model", model_data)
        zf.writestr("Metadata/plate_1.gcode", complete_gcode)
        zf.writestr("Metadata/plate_1.gcode.md5", gcode_md5)
        zf.writestr("Metadata/slice_info.config", slice_info)
        zf.writestr("Metadata/plate_1.json", plate_json)
        zf.writestr("Metadata/model_settings.config", model_settings)
        zf.writestr(
            "Metadata/_rels/model_settings.config.rels", _MODEL_SETTINGS_RELS
        )
        zf.writestr(
            "Metadata/cut_information.xml",
            '<?xml version="1.0" encoding="UTF-8"?>\n<cut_information/>',
        )
        zf.writestr(
            "Metadata/filament_sequence.json", '{"filament_sequence": [0]}'
        )
        zf.writestr("Metadata/project_settings.config", "{}")
        for name, data in thumbnails.items():
            zf.writestr(name, data)

    file_size = os.path.getsize(output_path)
    file_md5 = hashlib.md5(  # noqa: S324
        Path(output_path).read_bytes()
    ).hexdigest()

    logger.info(
        "Built Bambu 3MF: %s (%d bytes, %d layers)",
        output_path,
        file_size,
        total_layers,
    )

    return Bambu3MFResult(
        output_path=output_path,
        total_layers=total_layers,
        max_z=max_z,
        file_size=file_size,
        md5=file_md5,
        est_print_time_sec=est_time_sec,
    )


def _reset_cache() -> None:
    """Reset lazy singletons — for testing only."""
    global _a1_start_gcode, _a1_end_gcode  # noqa: PLW0603
    _a1_start_gcode = None
    _a1_end_gcode = None
