"""FDM 3D printing file metadata extraction for Kiln.

Parses file headers and structure for FDM printing files to extract metadata
(dimensions, estimated time, layer count, material hints, slicer info,
filament usage, temperatures) so agents can reason about files without manual
inspection.

Supported formats:

- **G-code**: ``.gcode``, ``.gco``, ``.g`` (header comment parsing)
- **3MF**: ``.3mf`` (ZIP with XML metadata)
- **UFP**: ``.ufp`` (Ultimaker format package)
- **STL**: ``.stl`` (mesh only, limited metadata — file size and binary/ASCII)

Usage::

    from kiln.file_metadata import extract_metadata

    meta = extract_metadata("/path/to/file.gcode")
    print(meta.file_type, meta.estimated_time_seconds)
    print(meta.to_dict())
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

_MAX_HEADER_LINES: int = 300


# ---------------------------------------------------------------------------
# File type / format mappings
# ---------------------------------------------------------------------------

_GCODE_EXTENSIONS: dict[str, str] = {
    ".gcode": "gcode",
    ".gco": "gcode",
    ".g": "gcode",
}

_3MF_EXTENSIONS: dict[str, str] = {
    ".3mf": "3mf",
}

_UFP_EXTENSIONS: dict[str, str] = {
    ".ufp": "ufp",
}

_STL_EXTENSIONS: dict[str, str] = {
    ".stl": "stl",
}

_ALL_EXTENSIONS: frozenset[str] = frozenset(
    set(_GCODE_EXTENSIONS) | set(_3MF_EXTENSIONS) | set(_UFP_EXTENSIONS) | set(_STL_EXTENSIONS)
)


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class FileMetadata:
    """Structured metadata extracted from an FDM 3D printing file."""

    file_path: str
    file_type: str = "unknown"
    file_format: str | None = None
    file_size_bytes: int = 0
    estimated_time_seconds: int | None = None
    layer_count: int | None = None
    dimensions_mm: dict[str, float] | None = None
    material_hint: str | None = None
    slicer_hint: str | None = None
    created_at: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dict, omitting ``None`` values."""
        raw = asdict(self)
        return {k: v for k, v in raw.items() if v is not None}


# ---------------------------------------------------------------------------
# G-code header patterns (works for FDM G-code from all major slicers)
# ---------------------------------------------------------------------------

_RE_ESTIMATED_TIME = re.compile(
    r";\s*estimated\s*(?:printing\s*)?time\s*[:=]\s*(.+)",
    re.IGNORECASE,
)
_RE_LAYER_COUNT = re.compile(
    r";\s*(?:layer\s*count|total\s*layers)\s*[:=]\s*(\d+)",
    re.IGNORECASE,
)
_RE_MATERIAL = re.compile(
    r";\s*(?:material|filament_type)\s*[:=]\s*(.+)",
    re.IGNORECASE,
)
_RE_SLICER = re.compile(
    r";\s*(?:slicer|generated\s*(?:by|with))\s*[:=]?\s*(.+)",
    re.IGNORECASE,
)
_RE_DIMENSIONS = re.compile(
    r";\s*dimensions?\s*[:=]\s*(.+)",
    re.IGNORECASE,
)
_RE_BOUNDS_X = re.compile(
    r";\s*(?:x_min|min_x|bounds_x)\s*[:=]\s*(-?\d+\.?\d*)\s*[-,to]+\s*(-?\d+\.?\d*)",
    re.IGNORECASE,
)
_RE_BOUNDS_Y = re.compile(
    r";\s*(?:y_min|min_y|bounds_y)\s*[:=]\s*(-?\d+\.?\d*)\s*[-,to]+\s*(-?\d+\.?\d*)",
    re.IGNORECASE,
)
_RE_BOUNDS_Z = re.compile(
    r";\s*(?:z_min|min_z|bounds_z)\s*[:=]\s*(-?\d+\.?\d*)\s*[-,to]+\s*(-?\d+\.?\d*)",
    re.IGNORECASE,
)

# FDM-specific header patterns
_RE_FILAMENT_USED = re.compile(
    r";\s*filament\s*(?:used|_used)\s*[:=]\s*(\d+\.?\d*)\s*(mm|g|m)?",
    re.IGNORECASE,
)
_RE_NOZZLE_DIAMETER = re.compile(
    r";\s*nozzle_diameter\s*[:=]\s*(\d+\.?\d*)",
    re.IGNORECASE,
)
_RE_LAYER_HEIGHT = re.compile(
    r";\s*layer_height\s*[:=]\s*(\d+\.?\d*)",
    re.IGNORECASE,
)
_RE_INFILL = re.compile(
    r";\s*(?:fill_density|infill_density|infill)\s*[:=]\s*(\d+\.?\d*)\s*%?",
    re.IGNORECASE,
)
_RE_PRINT_SPEED = re.compile(
    r";\s*(?:print_speed|speed)\s*[:=]\s*(\d+\.?\d*)",
    re.IGNORECASE,
)
_RE_BED_TEMP = re.compile(
    r";\s*bed_temperature\s*[:=]\s*(\d+\.?\d*)",
    re.IGNORECASE,
)
_RE_HOTEND_TEMP = re.compile(
    r";\s*(?:temperature|nozzle_temperature)\s*[:=]\s*(\d+\.?\d*)",
    re.IGNORECASE,
)
_RE_FEED_RATE = re.compile(
    r";\s*feed\s*rate\s*[:=]\s*(\d+\.?\d*)",
    re.IGNORECASE,
)
_RE_PRINTER_MODEL = re.compile(
    r";\s*printer_model\s*[:=]\s*(.+)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Time string parsing
# ---------------------------------------------------------------------------


def _parse_time_string(raw: str) -> int | None:
    """Parse time strings like ``1h 42m 30s``, ``6150``, ``1 hours 42 minutes``.

    :returns: Total seconds, or ``None`` if unparseable.
    """
    raw = raw.strip()
    if not raw:
        return None

    # Pure integer seconds
    if re.fullmatch(r"\d+", raw):
        try:
            return int(raw)
        except ValueError:
            return None

    total = 0
    found = False

    # Days
    m = re.search(r"(\d+)\s*d(?:ays?)?", raw, re.IGNORECASE)
    if m:
        total += int(m.group(1)) * 86400
        found = True

    # Hours
    m = re.search(r"(\d+)\s*h(?:ours?)?", raw, re.IGNORECASE)
    if m:
        total += int(m.group(1)) * 3600
        found = True

    # Minutes
    m = re.search(r"(\d+)\s*m(?:inutes?|in)?(?:\b|$)", raw, re.IGNORECASE)
    if m:
        total += int(m.group(1)) * 60
        found = True

    # Seconds
    m = re.search(r"(\d+)\s*s(?:econds?|ec)?(?:\b|$)", raw, re.IGNORECASE)
    if m:
        total += int(m.group(1))
        found = True

    return total if found else None


# ---------------------------------------------------------------------------
# Dimension parsing from header comment
# ---------------------------------------------------------------------------


def _parse_dimensions_string(raw: str) -> dict[str, float] | None:
    """Parse dimension strings like ``100x200x50`` or ``100 x 200 x 50``.

    :returns: Dict with ``x``, ``y``, ``z`` keys (or fewer), or ``None``.
    """
    raw = raw.strip()
    parts = re.split(r"\s*[xX,]\s*", raw)
    dims: dict[str, float] = {}
    keys = ("x", "y", "z")
    for i, part in enumerate(parts):
        if i >= len(keys):
            break
        try:
            dims[keys[i]] = float(part)
        except ValueError:
            break
    return dims if dims else None


# ---------------------------------------------------------------------------
# G-code header extraction (FDM printers)
# ---------------------------------------------------------------------------


def _extract_gcode_metadata(file_path: str) -> FileMetadata:
    """Parse G-code header comments for FDM printer metadata.

    Scans the first :data:`_MAX_HEADER_LINES` lines for embedded comments
    from PrusaSlicer, OrcaSlicer, Cura, Simplify3D, BambuStudio, and others.
    """
    ext = os.path.splitext(file_path)[1].lower()
    file_format = _GCODE_EXTENSIONS.get(ext, ext.lstrip("."))

    meta = FileMetadata(
        file_path=file_path,
        file_type="gcode",
        file_format=file_format,
        file_size_bytes=_safe_file_size(file_path),
        created_at=_safe_created_at(file_path),
    )

    try:
        lines = _read_header_lines(file_path)
    except OSError as exc:
        logger.warning("Could not read file for metadata: %s", exc)
        return meta

    for line in lines:
        stripped = line.strip()
        if not stripped or not stripped.startswith(";"):
            continue

        # Estimated time
        if meta.estimated_time_seconds is None:
            m = _RE_ESTIMATED_TIME.match(stripped)
            if m:
                meta.estimated_time_seconds = _parse_time_string(m.group(1))

        # Layer count
        if meta.layer_count is None:
            m = _RE_LAYER_COUNT.match(stripped)
            if m:
                with contextlib.suppress(ValueError):
                    meta.layer_count = int(m.group(1))

        # Material
        if meta.material_hint is None:
            m = _RE_MATERIAL.match(stripped)
            if m:
                meta.material_hint = m.group(1).strip()

        # Slicer
        if meta.slicer_hint is None:
            m = _RE_SLICER.match(stripped)
            if m:
                meta.slicer_hint = m.group(1).strip()

        # Dimensions from explicit comment
        if meta.dimensions_mm is None:
            m = _RE_DIMENSIONS.match(stripped)
            if m:
                meta.dimensions_mm = _parse_dimensions_string(m.group(1))

        # --- FDM-specific extras ---

        # Filament used
        if "filament_used" not in meta.extra:
            m = _RE_FILAMENT_USED.match(stripped)
            if m:
                try:
                    val = float(m.group(1))
                    unit = (m.group(2) or "mm").lower()
                    if unit == "m":
                        val *= 1000.0
                        unit = "mm"
                    meta.extra["filament_used"] = val
                    meta.extra["filament_used_unit"] = unit
                except ValueError:
                    pass

        # Nozzle diameter
        if "nozzle_diameter" not in meta.extra:
            m = _RE_NOZZLE_DIAMETER.match(stripped)
            if m:
                with contextlib.suppress(ValueError):
                    meta.extra["nozzle_diameter"] = float(m.group(1))

        # Layer height
        if "layer_height" not in meta.extra:
            m = _RE_LAYER_HEIGHT.match(stripped)
            if m:
                with contextlib.suppress(ValueError):
                    meta.extra["layer_height"] = float(m.group(1))

        # Infill density
        if "infill_pct" not in meta.extra:
            m = _RE_INFILL.match(stripped)
            if m:
                with contextlib.suppress(ValueError):
                    meta.extra["infill_pct"] = float(m.group(1))

        # Print speed
        if "print_speed" not in meta.extra:
            m = _RE_PRINT_SPEED.match(stripped)
            if m:
                with contextlib.suppress(ValueError):
                    meta.extra["print_speed"] = float(m.group(1))

        # Bed temperature
        if "bed_temp" not in meta.extra:
            m = _RE_BED_TEMP.match(stripped)
            if m:
                with contextlib.suppress(ValueError):
                    meta.extra["bed_temp"] = float(m.group(1))

        # Hotend temperature
        if "hotend_temp" not in meta.extra:
            m = _RE_HOTEND_TEMP.match(stripped)
            if m:
                with contextlib.suppress(ValueError):
                    meta.extra["hotend_temp"] = float(m.group(1))

        # Feed rate
        if "feed_rate" not in meta.extra:
            m = _RE_FEED_RATE.match(stripped)
            if m:
                with contextlib.suppress(ValueError):
                    meta.extra["feed_rate"] = float(m.group(1))

        # Printer model
        if "printer_model" not in meta.extra:
            m = _RE_PRINTER_MODEL.match(stripped)
            if m:
                meta.extra["printer_model"] = m.group(1).strip()

    # Try to extract bounds from explicit bound comments
    if meta.dimensions_mm is None:
        dims = _extract_bounds_from_lines(lines)
        if dims:
            meta.dimensions_mm = dims

    return meta


def _extract_bounds_from_lines(lines: list[str]) -> dict[str, float] | None:
    """Extract X/Y/Z dimensions from bound comments like ``; bounds_x = 0 - 100``."""
    dims: dict[str, float] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith(";"):
            continue

        for axis, pattern in (("x", _RE_BOUNDS_X), ("y", _RE_BOUNDS_Y), ("z", _RE_BOUNDS_Z)):
            if axis not in dims:
                m = pattern.match(stripped)
                if m:
                    try:
                        lo = float(m.group(1))
                        hi = float(m.group(2))
                        dims[axis] = abs(hi - lo)
                    except ValueError:
                        pass

    return dims if dims else None


# ---------------------------------------------------------------------------
# 3MF metadata extraction
# ---------------------------------------------------------------------------


def _extract_3mf_metadata(file_path: str) -> FileMetadata:
    """Extract metadata from a 3MF file (ZIP archive with XML metadata).

    3MF files follow the Open Packaging Convention.  The core metadata lives
    in ``3D/3dmodel.model`` (OPC part) and print settings may be in
    ``Metadata/`` entries.  This function extracts what is available from
    the OPC relationships and core model XML.
    """
    meta = FileMetadata(
        file_path=file_path,
        file_type="3mf",
        file_format="3mf",
        file_size_bytes=_safe_file_size(file_path),
        created_at=_safe_created_at(file_path),
    )

    try:
        with zipfile.ZipFile(file_path, "r") as zf:
            # Try to read the core model for basic info
            model_path = _find_3mf_model_path(zf)
            if model_path is not None:
                _parse_3mf_model(zf, model_path, meta)

            # Try slicer-specific metadata (PrusaSlicer, BambuStudio, etc.)
            _parse_3mf_slicer_metadata(zf, meta)

    except (zipfile.BadZipFile, OSError, KeyError) as exc:
        logger.warning("Could not parse 3MF file: %s", exc)

    return meta


def _find_3mf_model_path(zf: zipfile.ZipFile) -> str | None:
    """Locate the 3D model part inside the 3MF ZIP.

    Checks the standard path ``3D/3dmodel.model`` first, then falls back to
    scanning the archive for any ``.model`` file.
    """
    standard = "3D/3dmodel.model"
    if standard in zf.namelist():
        return standard

    for name in zf.namelist():
        if name.lower().endswith(".model"):
            return name

    return None


def _parse_3mf_model(
    zf: zipfile.ZipFile,
    model_path: str,
    meta: FileMetadata,
) -> None:
    """Parse the 3MF core model XML for metadata elements."""
    _3MF_NS = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"

    try:
        with zf.open(model_path) as fh:
            tree = ET.parse(fh)
        root = tree.getroot()

        # Extract <metadata> elements
        for md_elem in root.iter(f"{{{_3MF_NS}}}metadata"):
            name = (md_elem.get("name") or "").lower()
            value = (md_elem.text or "").strip()
            if not value:
                continue

            if name == "application" and meta.slicer_hint is None:
                meta.slicer_hint = value
            elif name in ("material", "filament") and meta.material_hint is None:
                meta.material_hint = value

        # Also try without namespace (some slicers omit it)
        for md_elem in root.iter("metadata"):
            name = (md_elem.get("name") or "").lower()
            value = (md_elem.text or "").strip()
            if not value:
                continue

            if name == "application" and meta.slicer_hint is None:
                meta.slicer_hint = value
            elif name in ("material", "filament") and meta.material_hint is None:
                meta.material_hint = value

    except (ET.ParseError, OSError) as exc:
        logger.warning("Failed to parse 3MF model XML: %s", exc)


def _parse_3mf_slicer_metadata(zf: zipfile.ZipFile, meta: FileMetadata) -> None:
    """Parse slicer-specific metadata files within the 3MF archive.

    PrusaSlicer stores config in ``Metadata/Slic3r_PE.config``;
    BambuStudio uses ``Metadata/plate_*.json`` and ``Metadata/project_settings.config``.
    """
    for name in zf.namelist():
        lower = name.lower()

        # PrusaSlicer / OrcaSlicer config inside 3MF
        if lower.endswith(".config") and "metadata" in lower:
            try:
                with zf.open(name) as fh:
                    content = fh.read().decode("utf-8", errors="replace")
                _parse_config_text(content, meta)
            except (OSError, KeyError):
                pass


def _parse_config_text(content: str, meta: FileMetadata) -> None:
    """Parse key=value slicer config text for metadata fields."""
    for line in content.splitlines()[:200]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        # filament_type = PLA
        if meta.material_hint is None:
            m = re.match(r"filament_type\s*=\s*(.+)", stripped, re.IGNORECASE)
            if m:
                meta.material_hint = m.group(1).strip()

        # layer_height = 0.2
        if "layer_height" not in meta.extra:
            m = re.match(r"layer_height\s*=\s*(\d+\.?\d*)", stripped, re.IGNORECASE)
            if m:
                with contextlib.suppress(ValueError):
                    meta.extra["layer_height"] = float(m.group(1))

        # nozzle_diameter = 0.4
        if "nozzle_diameter" not in meta.extra:
            m = re.match(r"nozzle_diameter\s*=\s*(\d+\.?\d*)", stripped, re.IGNORECASE)
            if m:
                with contextlib.suppress(ValueError):
                    meta.extra["nozzle_diameter"] = float(m.group(1))

        # printer_model = MK3S
        if "printer_model" not in meta.extra:
            m = re.match(r"printer_model\s*=\s*(.+)", stripped, re.IGNORECASE)
            if m:
                meta.extra["printer_model"] = m.group(1).strip()


# ---------------------------------------------------------------------------
# UFP metadata extraction
# ---------------------------------------------------------------------------


def _extract_ufp_metadata(file_path: str) -> FileMetadata:
    """Extract metadata from a UFP file (Ultimaker Format Package).

    UFP is a ZIP archive containing G-code plus metadata, thumbnails, etc.
    The G-code is typically at ``/3D/model.gcode`` inside the package.
    """
    meta = FileMetadata(
        file_path=file_path,
        file_type="ufp",
        file_format="ufp",
        file_size_bytes=_safe_file_size(file_path),
        created_at=_safe_created_at(file_path),
    )

    try:
        with zipfile.ZipFile(file_path, "r") as zf:
            # Find G-code file inside the package
            gcode_path: str | None = None
            for name in zf.namelist():
                if name.lower().endswith((".gcode", ".gco")):
                    gcode_path = name
                    break

            if gcode_path is not None:
                with zf.open(gcode_path) as fh:
                    lines: list[str] = []
                    for i, raw_line in enumerate(fh):
                        if i >= _MAX_HEADER_LINES:
                            break
                        lines.append(raw_line.decode("utf-8", errors="replace"))

                # Re-use gcode parsing on extracted lines
                gcode_meta = _extract_gcode_metadata_from_lines(lines)
                meta.estimated_time_seconds = gcode_meta.estimated_time_seconds
                meta.layer_count = gcode_meta.layer_count
                meta.dimensions_mm = gcode_meta.dimensions_mm
                meta.material_hint = gcode_meta.material_hint
                meta.slicer_hint = gcode_meta.slicer_hint
                meta.extra = gcode_meta.extra

    except (zipfile.BadZipFile, OSError, KeyError) as exc:
        logger.warning("Could not parse UFP file: %s", exc)

    return meta


def _extract_gcode_metadata_from_lines(lines: list[str]) -> FileMetadata:
    """Parse G-code metadata from pre-read lines (used by UFP extraction).

    Returns a :class:`FileMetadata` with gcode-type fields populated.
    """
    meta = FileMetadata(file_path="", file_type="gcode", file_format="gcode")

    for line in lines:
        stripped = line.strip()
        if not stripped or not stripped.startswith(";"):
            continue

        if meta.estimated_time_seconds is None:
            m = _RE_ESTIMATED_TIME.match(stripped)
            if m:
                meta.estimated_time_seconds = _parse_time_string(m.group(1))

        if meta.layer_count is None:
            m = _RE_LAYER_COUNT.match(stripped)
            if m:
                with contextlib.suppress(ValueError):
                    meta.layer_count = int(m.group(1))

        if meta.material_hint is None:
            m = _RE_MATERIAL.match(stripped)
            if m:
                meta.material_hint = m.group(1).strip()

        if meta.slicer_hint is None:
            m = _RE_SLICER.match(stripped)
            if m:
                meta.slicer_hint = m.group(1).strip()

        if meta.dimensions_mm is None:
            m = _RE_DIMENSIONS.match(stripped)
            if m:
                meta.dimensions_mm = _parse_dimensions_string(m.group(1))

        if "filament_used" not in meta.extra:
            m = _RE_FILAMENT_USED.match(stripped)
            if m:
                try:
                    val = float(m.group(1))
                    unit = (m.group(2) or "mm").lower()
                    if unit == "m":
                        val *= 1000.0
                        unit = "mm"
                    meta.extra["filament_used"] = val
                    meta.extra["filament_used_unit"] = unit
                except ValueError:
                    pass

        if "nozzle_diameter" not in meta.extra:
            m = _RE_NOZZLE_DIAMETER.match(stripped)
            if m:
                with contextlib.suppress(ValueError):
                    meta.extra["nozzle_diameter"] = float(m.group(1))

        if "layer_height" not in meta.extra:
            m = _RE_LAYER_HEIGHT.match(stripped)
            if m:
                with contextlib.suppress(ValueError):
                    meta.extra["layer_height"] = float(m.group(1))

        if "infill_pct" not in meta.extra:
            m = _RE_INFILL.match(stripped)
            if m:
                with contextlib.suppress(ValueError):
                    meta.extra["infill_pct"] = float(m.group(1))

        if "print_speed" not in meta.extra:
            m = _RE_PRINT_SPEED.match(stripped)
            if m:
                with contextlib.suppress(ValueError):
                    meta.extra["print_speed"] = float(m.group(1))

        if "bed_temp" not in meta.extra:
            m = _RE_BED_TEMP.match(stripped)
            if m:
                with contextlib.suppress(ValueError):
                    meta.extra["bed_temp"] = float(m.group(1))

        if "hotend_temp" not in meta.extra:
            m = _RE_HOTEND_TEMP.match(stripped)
            if m:
                with contextlib.suppress(ValueError):
                    meta.extra["hotend_temp"] = float(m.group(1))

        if "feed_rate" not in meta.extra:
            m = _RE_FEED_RATE.match(stripped)
            if m:
                with contextlib.suppress(ValueError):
                    meta.extra["feed_rate"] = float(m.group(1))

        if "printer_model" not in meta.extra:
            m = _RE_PRINTER_MODEL.match(stripped)
            if m:
                meta.extra["printer_model"] = m.group(1).strip()

    if meta.dimensions_mm is None:
        dims = _extract_bounds_from_lines(lines)
        if dims:
            meta.dimensions_mm = dims

    return meta


# ---------------------------------------------------------------------------
# STL metadata extraction
# ---------------------------------------------------------------------------


def _extract_stl_metadata(file_path: str) -> FileMetadata:
    """Extract minimal metadata from an STL file.

    STL files contain only mesh geometry (no slicer metadata, no print
    settings).  We detect binary vs ASCII format and report file size.
    """
    meta = FileMetadata(
        file_path=file_path,
        file_type="stl",
        file_format="stl",
        file_size_bytes=_safe_file_size(file_path),
        created_at=_safe_created_at(file_path),
    )

    try:
        with open(file_path, "rb") as fh:
            header = fh.read(80)

        if header[:5] == b"solid":
            meta.extra["stl_format"] = "ascii"
        else:
            meta.extra["stl_format"] = "binary"
    except OSError as exc:
        logger.warning("Could not read STL file: %s", exc)

    return meta


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def extract_metadata(file_path: str) -> FileMetadata:
    """Extract metadata from an FDM printing file, auto-detecting type by extension.

    :param file_path: Path to a printing file (.gcode, .3mf, .ufp, .stl).
    :returns: A :class:`FileMetadata` with whatever fields could be parsed.
        Never raises on parse errors -- returns minimal metadata on failure.
    """
    if not file_path:
        return FileMetadata(file_path=file_path or "", file_type="unknown")

    if not os.path.isfile(file_path):
        logger.warning("File not found: %s", file_path)
        return FileMetadata(
            file_path=file_path,
            file_type="unknown",
            file_size_bytes=0,
        )

    ext = os.path.splitext(file_path)[1].lower()

    try:
        if ext in _GCODE_EXTENSIONS:
            return _extract_gcode_metadata(file_path)
        elif ext in _3MF_EXTENSIONS:
            return _extract_3mf_metadata(file_path)
        elif ext in _UFP_EXTENSIONS:
            return _extract_ufp_metadata(file_path)
        elif ext in _STL_EXTENSIONS:
            return _extract_stl_metadata(file_path)
        else:
            # Unknown format -- return basic file info
            return FileMetadata(
                file_path=file_path,
                file_type="unknown",
                file_format=ext.lstrip(".") if ext else None,
                file_size_bytes=_safe_file_size(file_path),
                created_at=_safe_created_at(file_path),
            )
    except Exception as exc:
        logger.warning("Unexpected error extracting file metadata: %s", exc)
        return FileMetadata(
            file_path=file_path,
            file_type="unknown",
            file_size_bytes=_safe_file_size(file_path),
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_header_lines(file_path: str, *, max_lines: int = _MAX_HEADER_LINES) -> list[str]:
    """Read the first *max_lines* lines from a text file."""
    lines: list[str] = []
    with open(file_path, errors="replace") as fh:
        for i, line in enumerate(fh):
            if i >= max_lines:
                break
            lines.append(line)
    return lines


def _safe_file_size(file_path: str) -> int:
    """Return file size in bytes, or 0 on error."""
    try:
        return os.path.getsize(file_path)
    except OSError:
        return 0


def _safe_created_at(file_path: str) -> str | None:
    """Return file creation/modification time as ISO-8601 string, or ``None``."""
    try:
        stat = os.stat(file_path)
        # Use birth time if available (macOS), otherwise mtime
        ts = getattr(stat, "st_birthtime", None) or stat.st_mtime
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except OSError:
        return None
