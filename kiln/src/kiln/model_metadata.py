"""3MF model metadata extraction for Kiln.

Parses .3mf files (ZIP archives containing XML) to extract material
information, colors, and print settings metadata.

A .3mf file is a ZIP archive with (at minimum):

    3D/3dmodel.model        — OPC part containing the 3D model XML
    Metadata/*.xml          — Optional print settings, thumbnails, etc.
    [Content_Types].xml     — OPC content types

The ``<basematerials>`` element in ``3dmodel.model`` lists the materials
used by the model, each with a name and display color (sRGB hex).

Example::

    from kiln.model_metadata import extract_3mf_metadata

    meta = extract_3mf_metadata("benchy.3mf")
    print(meta["materials"])   # ["PLA", "PETG"]
    print(meta["colors"])      # ["#FF0000", "#00FF00"]
"""

from __future__ import annotations

import logging
import os
import xml.etree.ElementTree as ET
import zipfile
from typing import Any

logger = logging.getLogger(__name__)

# The primary model file inside a 3MF archive.
_MODEL_PATH = "3D/3dmodel.model"

# 3MF core namespace.
_NS_3MF = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"


def extract_3mf_metadata(file_path: str) -> dict[str, Any]:
    """Extract material and print-settings metadata from a .3mf file.

    Args:
        file_path: Path to a ``.3mf`` file.

    Returns:
        A dict with keys:
            - ``materials``: list of material name strings (may be empty).
            - ``colors``: list of sRGB color strings, e.g. ``"#FF0000"``
              (may be empty, parallel to materials).
            - ``print_settings``: dict of print setting key/value pairs
              extracted from Metadata entries, or ``None`` if absent.

    The function never raises on bad input — it returns empty/None values
    with a logged warning so callers can proceed gracefully.
    """
    result: dict[str, Any] = {
        "materials": [],
        "colors": [],
        "print_settings": None,
    }

    # --- Validate path ---
    if not file_path or not os.path.isfile(file_path):
        logger.warning("3MF file not found: %s", file_path)
        return result

    if not file_path.lower().endswith(".3mf"):
        logger.warning("File does not have .3mf extension: %s", file_path)
        return result

    # --- Open ZIP ---
    try:
        zf = zipfile.ZipFile(file_path, "r")
    except (zipfile.BadZipFile, OSError) as exc:
        logger.warning("Cannot open 3MF file as ZIP: %s (%s)", file_path, exc)
        return result

    with zf:
        # --- Extract materials from 3dmodel.model ---
        materials, colors = _parse_model_materials(zf)
        result["materials"] = materials
        result["colors"] = colors

        # --- Extract print settings from Metadata/ entries ---
        result["print_settings"] = _parse_metadata_settings(zf)

    return result


def _parse_model_materials(zf: zipfile.ZipFile) -> tuple[list[str], list[str]]:
    """Parse <basematerials> from 3D/3dmodel.model inside the ZIP."""
    materials: list[str] = []
    colors: list[str] = []

    if _MODEL_PATH not in zf.namelist():
        logger.debug("No %s found in 3MF archive", _MODEL_PATH)
        return materials, colors

    try:
        with zf.open(_MODEL_PATH) as f:
            tree = ET.parse(f)
    except (ET.ParseError, OSError) as exc:
        logger.warning("Failed to parse %s XML: %s", _MODEL_PATH, exc)
        return materials, colors

    root = tree.getroot()

    # Find <basematerials> — could be namespaced or not.
    for base_mat in _find_elements(root, "basematerials"):
        for base in _find_elements(base_mat, "base"):
            name = base.get("name", "").strip()
            color = base.get("displaycolor", "").strip()
            if name:
                materials.append(name)
                colors.append(color if color else "")

    return materials, colors


def _parse_metadata_settings(zf: zipfile.ZipFile) -> dict[str, str] | None:
    """Extract print settings from Metadata/ XML files in the archive.

    Many slicers (PrusaSlicer, OrcaSlicer, Cura) embed print settings as
    ``<metadata>`` elements inside the model XML or as separate files under
    ``Metadata/``.  This function collects key-value pairs from both sources.
    """
    settings: dict[str, str] = {}

    # --- Check model-level <metadata> elements ---
    if _MODEL_PATH in zf.namelist():
        try:
            with zf.open(_MODEL_PATH) as f:
                tree = ET.parse(f)
            root = tree.getroot()
            for meta_elem in _find_elements(root, "metadata"):
                key = meta_elem.get("name", "").strip()
                value = (meta_elem.text or "").strip()
                if key and value:
                    settings[key] = value
        except (ET.ParseError, OSError):
            pass  # Already warned in _parse_model_materials

    # --- Check Metadata/ directory entries ---
    metadata_files = [
        name for name in zf.namelist() if name.lower().startswith("metadata/") and name.lower().endswith(".xml")
    ]
    for meta_file in metadata_files:
        try:
            with zf.open(meta_file) as f:
                tree = ET.parse(f)
            root = tree.getroot()
            # Walk all elements looking for key/value-like attributes or text
            for elem in root.iter():
                key = elem.get("name") or elem.get("key") or ""
                value = elem.get("value") or (elem.text or "").strip()
                if key.strip() and value.strip():
                    settings[key.strip()] = value.strip()
        except (ET.ParseError, OSError) as exc:
            logger.debug("Failed to parse metadata file %s: %s", meta_file, exc)

    return settings if settings else None


def _find_elements(parent: ET.Element, local_name: str) -> list[ET.Element]:
    """Find child elements by local name, ignoring XML namespace prefixes.

    This handles both namespaced (``{http://...}basematerials``) and
    non-namespaced (``basematerials``) elements in the 3MF XML.
    """
    results: list[ET.Element] = []
    # Try with 3MF namespace
    results.extend(parent.findall(f"{{{_NS_3MF}}}{local_name}"))
    # Try without namespace
    results.extend(parent.findall(local_name))
    # Also search recursively for deeply nested elements
    for elem in parent.iter():
        tag = elem.tag
        # Strip namespace if present
        if "}" in str(tag):
            tag = str(tag).split("}", 1)[1]
        if tag == local_name and elem not in results:
            results.append(elem)
    return results
